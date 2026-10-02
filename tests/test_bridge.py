import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from o2gateway.cloud.base import CloudItemMetadata

from bridge import security
from bridge.backend import GuardedTransport, MemoryCache, metadata, read_retry
from bridge.security import BridgeError, SessionStore, Vault, remote_path, session_from_cookie
from bridge.service import Hub, create_app, safe_error


class Keyring:
    def __init__(self):
        self.values = {}

    def get_password(self, service, name):
        return self.values.get((service, name))

    def set_password(self, service, name, value):
        self.values[service, name] = value

    def delete_password(self, service, name):
        self.values.pop((service, name), None)


class Store:
    def __init__(self, contents=None, corrupt=False, delay=0):
        self.contents = contents or {}
        self.corrupt, self.delay = corrupt, delay
        self.deleted = []
        self.api = SimpleNamespace(session_expired_at=lambda: None)
        self.ranges = []

    def item(self, path):
        content = self.contents[path]
        return CloudItemMetadata(
            id=path,
            name=Path(path).name,
            path=path,
            type="file",
            size=len(content),
            etag=hashlib.sha256(content).hexdigest(),
            modified_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    async def list(self, path):
        return [self.item(key) for key in self.contents if str(Path(key).parent) == path]

    async def get_metadata(self, path):
        return self.item(path) if path in self.contents else None

    async def upload(self, path, local, overwrite=False):
        if path in self.contents:
            raise BridgeError("CONFLICT", "Existe")
        self.contents[path] = Path(local).read_bytes() + (b"corrupt" if self.corrupt else b"")
        return self.item(path)

    async def open_read(self, path, byte_range=None):
        offset = byte_range[0] if byte_range else 0
        self.ranges.append(offset)
        for i in range(offset, len(self.contents[path]), 2):
            await asyncio.sleep(self.delay)
            yield self.contents[path][i : i + 2]

    async def delete(self, path, soft_delete=True):
        assert soft_delete
        self.deleted.append(path)
        del self.contents[path]

    async def close(self):
        pass

    async def reset(self):
        pass


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setattr(security, "STATE", tmp_path / "state")
    monkeypatch.setenv("O2_DESKTOP_LOCAL_ROOTS", str(tmp_path))
    vault = Vault(Keyring())
    return Hub(Store({"/source.txt": b"hello cloud"}), SessionStore(vault), vault)


async def finish(hub, job):
    task = hub.tasks.get(job["id"])
    if task:
        await task
    return hub.jobs[job["id"]]


@pytest.mark.parametrize("path", ["../x", "/a/../b", "/a/./b", "/a\\b", "/x\x00"])
def test_invalid_cloud_paths_are_rejected(path):
    with pytest.raises(BridgeError):
        remote_path(path)


def test_root_is_protected():
    with pytest.raises(BridgeError, match="raíz"):
        remote_path("/", mutable=True)


def test_secrets_only_go_to_keyring(tmp_path):
    backend = Keyring()
    sessions = SessionStore(Vault(backend))
    session = session_from_cookie(
        "JSESSIONID=test-session; validationKey=test-key; analytics=ignored"
    )
    sessions.save(session)
    assert sessions.read().validation_key == "test-key"
    assert len(sessions.read().cookies) == 2
    assert list(tmp_path.iterdir()) == []
    sessions.delete()
    assert sessions.read() is None


def test_locked_vault_has_no_plaintext_fallback():
    class Locked(Keyring):
        def get_password(self, *args):
            raise RuntimeError("SECRET-DO-NOT-LOG")

    with pytest.raises(BridgeError) as error:
        Vault(Locked()).local_token()
    assert "SECRET" not in str(error.value)


def test_errors_and_metadata_do_not_expose_signed_tokens():
    assert "sensitive" not in str(safe_error(RuntimeError("validationKey=sensitive")))
    item = CloudItemMetadata(
        id="1", name="a", path="/a", type="file", raw={"downloadToken": "sensitive"}
    )
    assert "sensitive" not in str(metadata(item))


async def test_local_http_requires_token_and_rejects_browser_origins(hub):
    app = create_app(hub, "local-secret")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        assert (await client.get("/health")).status_code == 200
        body = {"operation": "status"}
        assert (await client.post("/rpc", json=body)).status_code == 401
        headers = {"Authorization": "Bearer local-secret", "Origin": "https://untrusted.test"}
        assert (await client.post("/rpc", json=body, headers=headers)).status_code == 401
        del headers["Origin"]
        assert (await client.post("/rpc", json=body, headers=headers)).status_code == 200


async def test_download_is_atomic_and_does_not_overwrite(hub, tmp_path):
    target = tmp_path / "download.txt"
    job = await hub.rpc("download", {"remote": "/source.txt", "local": str(target)})
    result = await finish(hub, job)
    assert result["state"] == "completed"
    assert target.read_bytes() == b"hello cloud"
    with pytest.raises(BridgeError):
        await hub.rpc("download", {"remote": "/source.txt", "local": str(target)})


async def test_cross_move_keeps_source_when_verification_fails(hub):
    destination = Store(corrupt=True)
    hub.backends["other"] = destination
    job = await hub.rpc(
        "copy",
        {
            "source": "/source.txt",
            "destination": "/target.txt",
            "destination_volume": "other",
            "move": True,
        },
    )
    result = await finish(hub, job)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "CHECKSUM_MISMATCH"
    assert hub.backend.deleted == []
    assert "/source.txt" in hub.backend.contents


async def test_cross_move_verifies_destination_before_trashing_source(hub):
    destination = Store()
    hub.backends["other"] = destination
    job = await hub.rpc(
        "copy",
        {
            "source": "/source.txt",
            "destination": "/target.txt",
            "destination_volume": "other",
            "move": True,
        },
    )
    result = await finish(hub, job)
    assert result["state"] == "completed"
    assert destination.contents["/target.txt"] == b"hello cloud"
    assert hub.backend.deleted == ["/source.txt"]


async def test_cancelled_download_resumes_at_verified_offset(hub, tmp_path):
    hub.backend.delay = 0.02
    job = await hub.rpc(
        "download", {"remote": "/source.txt", "local": str(tmp_path / "resume.txt")}
    )
    while hub.jobs[job["id"]]["bytes"] < 2:
        await asyncio.sleep(0.005)
    await hub.rpc("cancel", {"id": job["id"]})
    result = await finish(hub, job)
    assert result["state"] == "cancelled"
    offset = Path(result["partial"]).stat().st_size
    assert 0 < offset < len(b"hello cloud")
    await hub.rpc("resume", {"id": job["id"]})
    result = await finish(hub, job)
    assert result["state"] == "completed"
    assert offset in hub.backend.ranges
    assert (tmp_path / "resume.txt").read_bytes() == b"hello cloud"


async def test_download_pause_and_resume(hub, tmp_path):
    hub.backend.delay = 0.02
    job = await hub.rpc(
        "download", {"remote": "/source.txt", "local": str(tmp_path / "paused.txt")}
    )
    while hub.jobs[job["id"]]["bytes"] < 2:
        await asyncio.sleep(0.005)
    await hub.rpc("pause", {"id": job["id"]})
    done = hub.jobs[job["id"]]["bytes"]
    await asyncio.sleep(0.06)
    assert hub.jobs[job["id"]]["state"] == "paused"
    assert hub.jobs[job["id"]]["bytes"] == done
    await hub.rpc("resume", {"id": job["id"]})
    assert (await finish(hub, job))["state"] == "completed"


async def test_changed_source_is_not_resumed(hub, tmp_path):
    hub.backend.delay = 0.02
    job = await hub.rpc(
        "download", {"remote": "/source.txt", "local": str(tmp_path / "changed.txt")}
    )
    while hub.jobs[job["id"]]["bytes"] < 2:
        await asyncio.sleep(0.005)
    await hub.rpc("cancel", {"id": job["id"]})
    await finish(hub, job)
    hub.backend.contents["/source.txt"] = b"other bytes"
    await hub.rpc("resume", {"id": job["id"]})
    result = await finish(hub, job)
    assert result["state"] == "failed"
    assert result["error"]["code"] == "SOURCE_CHANGED"
    assert not (tmp_path / "changed.txt").exists()


async def test_download_resume_survives_service_restart(hub, tmp_path):
    hub.backend.delay = 0.02
    job = await hub.rpc(
        "download", {"remote": "/source.txt", "local": str(tmp_path / "restart.txt")}
    )
    while hub.jobs[job["id"]]["bytes"] < 2:
        await asyncio.sleep(0.005)
    await hub.rpc("cancel", {"id": job["id"]})
    await finish(hub, job)
    restored = Hub(hub.backend, hub.sessions, hub.vault)
    await restored.rpc("resume", {"id": job["id"]})
    assert (await finish(restored, job))["state"] == "completed"


async def test_download_credentials_do_not_reach_external_cdn():
    async def handler(request):
        assert "cookie" not in request.headers
        assert "authorization" not in request.headers
        return httpx.Response(200, content=b"data")

    async with httpx.AsyncClient(
        transport=GuardedTransport(httpx.MockTransport(handler))
    ) as client:
        response = await client.get(
            "https://cdn.example.test/file",
            headers={"Cookie": "JSESSIONID=sensitive", "Authorization": "oauth sensitive"},
        )
        assert response.content == b"data"


async def test_resume_rejects_server_ignoring_range():
    async with httpx.AsyncClient(
        transport=GuardedTransport(
            httpx.MockTransport(lambda r: httpx.Response(200, content=b"data"))
        )
    ) as client:
        with pytest.raises(BridgeError, match="reanudar"):
            await client.get("https://cloud.o2online.es/download", headers={"Range": "bytes=2-"})


async def test_reads_retry_transient_errors():
    attempts = 0

    async def call():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("offline")
        return "success"

    assert await read_retry(call) == "success"
    assert attempts == 2


async def test_memory_cache_never_persists_raw_tokens(tmp_path):
    cache = MemoryCache()
    item = CloudItemMetadata(id="1", name="a", path="/a", type="file", raw={"token": "sensitive"})
    await cache.put(item)
    assert await cache.get("/a") == item
    assert list(tmp_path.iterdir()) == []


async def test_download_retries_validation_errors_before_first_byte(monkeypatch):
    from o2gateway.operations.errors import CloudError

    from bridge.backend import open_read_retry

    attempts = []

    class Backend:
        async def open_read(self, path, byte_range=None):
            attempts.append(path)
            if len(attempts) == 1:
                raise CloudError("validation in progress")
            yield b"complete"

    async def no_sleep(seconds):
        pass

    monkeypatch.setattr("bridge.backend.asyncio.sleep", no_sleep)
    assert b"".join([c async for c in open_read_retry(Backend(), "/test")]) == b"complete"
    assert len(attempts) == 2


async def test_stream_failure_after_first_byte_is_never_replayed():
    from o2gateway.operations.errors import CloudError

    from bridge.backend import open_read_retry

    attempts = []

    class Backend:
        async def open_read(self, path, byte_range=None):
            attempts.append(path)
            yield b"partial"
            raise CloudError("connection failed")

    chunks = []
    with pytest.raises(CloudError):
        async for chunk in open_read_retry(Backend(), "/test"):
            chunks.append(chunk)
    assert chunks == [b"partial"] and len(attempts) == 1
