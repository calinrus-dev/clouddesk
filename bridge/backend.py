from __future__ import annotations

import asyncio
import contextvars
import logging
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Any, Protocol

import httpx
from o2gateway.cloud.base import CloudItemMetadata, parent_path
from o2gateway.o2.api import O2CloudApiClient
from o2gateway.o2.store import O2CloudFileStore
from o2gateway.operations.errors import CloudError
from o2gateway.settings import Settings

from .security import BridgeError, SessionStore

transfer_progress: contextvars.ContextVar = contextvars.ContextVar(
    "transfer_progress", default=None
)
transfer_control: contextvars.ContextVar = contextvars.ContextVar("transfer_control", default=None)


def metadata(item: CloudItemMetadata) -> dict:
    # raw contains signed URLs/download tokens: never return or persist it.
    return {
        "id": item.id,
        "name": item.name,
        "type": item.type,
        "path": item.path,
        "size": item.size,
        "modified": item.modified_at.isoformat() if item.modified_at else None,
        "content_type": item.content_type,
    }


class Backend(Protocol):
    async def list(self, path: str) -> list[CloudItemMetadata]: ...
    async def get_metadata(self, path: str) -> CloudItemMetadata | None: ...
    async def create_folder(self, path: str) -> CloudItemMetadata: ...
    async def upload(
        self, path: str, local: str, *, overwrite: bool = False
    ) -> CloudItemMetadata: ...
    async def open_read(self, path: str) -> AsyncIterator[bytes]: ...
    async def move(
        self, source: str, destination: str, *, overwrite: bool = False
    ) -> CloudItemMetadata: ...
    async def delete(self, path: str, *, soft_delete: bool = True) -> None: ...
    async def quota(self) -> Any: ...
    async def close(self) -> None: ...


class MemoryCache:
    """Upstream caches raw signed links. Keep the entire cache in RAM."""

    def __init__(self):
        self.items = {}

    async def get(self, path):
        record = self.items.get(path)
        return record[1] if record and record[0] > time.monotonic() else None

    async def put(self, item):
        self.items[item.path] = (time.monotonic() + 20, item)

    async def put_negative(self, path):
        self.items.pop(path, None)

    async def invalidate(self, *paths):
        for path in paths:
            self.items.pop(path, None)
            self.items.pop(parent_path(path), None)

    async def clear(self):
        self.items.clear()


class ProgressStream(httpx.AsyncByteStream):
    def __init__(self, stream, callback, total):
        self.stream, self.callback, self.total = stream, callback, total

    async def __aiter__(self):
        done = 0
        async for chunk in self.stream:
            control = transfer_control.get()
            if control:
                await control.gate.wait()
            yield chunk
            done += len(chunk)
            self.callback(done, self.total)

    async def aclose(self):
        await self.stream.aclose()


class GuardedTransport(httpx.AsyncBaseTransport):
    def __init__(self, inner=None):
        self.inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request):
        host = request.url.host
        if request.url.scheme != "https":
            raise BridgeError("UNSAFE_URL", "O2 ha devuelto una URL sin HTTPS.")
        # Binary download URLs may use a CDN. Never send O2's cookies or OAuth there.
        if host not in ("cloud.o2online.es", "upload.cloud.o2online.es"):
            for name in ("cookie", "authorization", "x-deviceid", "encryption-token"):
                request.headers.pop(name, None)
            if any(k.lower() == "validationkey" for k in request.url.params):
                raise BridgeError("UNSAFE_URL", "La URL de descarga expone la sesión de O2.")
            import ipaddress

            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                if host == "localhost" or host.endswith((".localhost", ".local")):
                    raise BridgeError("UNSAFE_URL", "O2 ha devuelto una URL local.") from None
            else:
                if not address.is_global:
                    raise BridgeError("UNSAFE_URL", "O2 ha devuelto una dirección privada.")
        callback = transfer_progress.get()
        if host == "upload.cloud.o2online.es" and callback and request.method == "POST":
            request.stream = ProgressStream(
                request.stream, callback, int(request.headers.get("content-length", "0"))
            )
        response = await self.inner.handle_async_request(request)
        requested_range = request.headers.get("range", "")
        if (
            requested_range
            and not requested_range.startswith("bytes=0-")
            and response.status_code < 300
        ):
            start = requested_range.split("=")[1].split("-")[0]
            if response.status_code != 206 or not response.headers.get(
                "content-range", ""
            ).startswith(f"bytes {start}-"):
                await response.stream.aclose()
                raise BridgeError(
                    "RANGE_UNSUPPORTED",
                    "El proveedor no admite reanudar esta descarga. El parcial se conserva.",
                )
        return response

    async def aclose(self):
        await self.inner.aclose()


class GatewayBackend:
    """Adapt the pinned, existing gateway; do not recreate O2's protocol."""

    def __init__(self, sessions: SessionStore):
        self.sessions = sessions
        self.tmp = tempfile.TemporaryDirectory(prefix="o2cloud-")
        self.settings = Settings(
            cloud_provider="o2",
            _env_file=None,
            o2_api_base_url="https://cloud.o2online.es/sapi/",
            o2_upload_base_url="https://upload.cloud.o2online.es/sapi/",
            o2_login_url="https://cloud.o2online.es/",
            cache_dir=self.tmp.name,
            upload_recent_cache_max_file_mb=0,
        )
        self.api = O2CloudApiClient(self.settings, sessions)
        self.api.client = httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=300),
            follow_redirects=False,
            transport=GuardedTransport(),
        )
        self.cache = MemoryCache()
        self.store = O2CloudFileStore(self.api, self.cache, self.settings)
        for name in ("o2gateway", "httpx", "httpcore"):
            logger = logging.getLogger(name)
            logger.handlers = [logging.NullHandler()]
            logger.propagate = False

    def __getattr__(self, name):
        return getattr(self.store, name)

    async def quota(self):
        return asdict(await self.store.quota())

    async def reset(self):
        for task in list(self.store._background_tasks):
            task.cancel()
        await asyncio.gather(*self.store._background_tasks, return_exceptions=True)
        self.store = O2CloudFileStore(self.api, MemoryCache(), self.settings)

    async def close(self):
        await self.reset()
        await self.api.close()
        self.tmp.cleanup()


async def read_retry(call):
    """Retry semantic reads only, even when SAPI implements a read using POST."""
    for attempt in range(3):
        try:
            return await call()
        except httpx.HTTPStatusError as error:
            if error.response.status_code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise
            delay = min(
                float(error.response.headers.get("retry-after", "1"))
                if error.response.headers.get("retry-after", "").isdigit()
                else 1,
                10,
            )
        except (httpx.TimeoutException, httpx.NetworkError):
            if attempt == 2:
                raise
            delay = 0.5 * 2**attempt
        await asyncio.sleep(delay)


async def open_read_retry(backend, path, byte_range=None):
    """Retry transient reads before the first byte, including O2's validation window.

    Never replay a stream after any bytes have been delivered: an explicit resume
    must validate metadata and Range before appending to its partial.
    """
    for attempt in range(5):
        delivered = False
        try:
            async for chunk in backend.open_read(path, byte_range):
                delivered = True
                yield chunk
            return
        except (CloudError, httpx.HTTPError) as error:
            if isinstance(error, CloudError):
                transient = error.status_code in (423, 429, 500, 502, 503, 504)
            elif isinstance(error, httpx.HTTPStatusError):
                transient = error.response.status_code in (429, 500, 502, 503, 504)
            else:
                transient = isinstance(error, (httpx.NetworkError, httpx.TimeoutException))
            if delivered or not transient or attempt == 4:
                raise
            await asyncio.sleep(attempt + 1)
