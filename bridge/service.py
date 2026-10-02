from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import signal
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from o2gateway.operations.errors import CloudError, CloudSessionExpired, CloudSessionMissing

from .backend import (
    GatewayBackend,
    metadata,
    open_read_retry,
    read_retry,
    transfer_control,
    transfer_progress,
)
from .rclone_backend import Rclone, RcloneBackend
from .security import (
    BridgeError,
    SessionStore,
    Vault,
    local_path,
    remote_path,
    state_dir,
)

log = logging.getLogger("o2desktop")


class Control:
    def __init__(self):
        self.gate = asyncio.Event()
        self.gate.set()
        self.process = None
        self.before_pause = "queued"

    def pause(self, job):
        self.before_pause = job["state"]
        self.gate.clear()
        if self.process and self.process.returncode is None:
            os.kill(self.process.pid, signal.SIGSTOP)
        job["state"] = "paused"

    def resume(self, job):
        if self.process and self.process.returncode is None:
            os.kill(self.process.pid, signal.SIGCONT)
        self.gate.set()
        job["state"] = self.before_pause


def safe_error(error: Exception) -> dict:
    if isinstance(error, BridgeError):
        return {"code": error.code, "message": error.message}
    if isinstance(error, (CloudSessionExpired, CloudSessionMissing)):
        return {"code": "SESSION_EXPIRED", "message": "Conecta O2 Cloud para renovar la sesión."}
    if isinstance(error, httpx.HTTPStatusError):
        status = error.response.status_code
        if status in (401, 403):
            return {
                "code": "SESSION_EXPIRED",
                "message": "O2 ha rechazado la sesión. Vuelve a conectar.",
            }
        return {"code": f"HTTP_{status}", "message": f"O2 respondió con el estado {status}."}
    if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
        return {
            "code": "NETWORK_ERROR",
            "message": "Error de conexión. Comprueba el listado antes de repetir una escritura.",
        }
    if isinstance(error, CloudError):
        messages = {
            404: "No se encuentra el archivo.",
            409: "El destino ya existe.",
            507: "No hay espacio en O2 Cloud.",
            423: "O2 todavía está validando el archivo.",
        }
        status = error.status_code
        return {
            "code": f"O2_{status}",
            "message": messages.get(
                status, "O2 no pudo completar la operación. Comprueba su estado antes de repetirla."
            ),
        }
    return {
        "code": "OPERATION_FAILED",
        "message": "No se pudo completar la operación. Revisa la conexión y el estado de O2.",
    }


class Hub:
    def __init__(self, backend, sessions, vault):
        self.backend, self.sessions, self.vault = backend, sessions, vault
        self.lock = asyncio.Lock()
        self.transfer_lock = asyncio.Lock()
        self.jobs: dict[str, dict] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self.auth = {"state": "idle", "error": None}
        self.login_task = None
        self.rclone = Rclone(vault)
        self.backends = {"o2": backend}
        self.controls = {}
        self.journal = state_dir() / "transfers.json"
        if self.journal.exists():
            try:
                for job in json.loads(self.journal.read_text()):
                    if job["state"] not in ("completed", "failed", "cancelled"):
                        job["state"] = "interrupted"
                        job["error"] = {
                            "code": "INTERRUPTED",
                            "message": "El servicio se reinició. Puedes reanudar las descargas con parcial válido.",
                        }
                    self.jobs[job["id"]] = job
            except (ValueError, KeyError, TypeError):
                pass

    def checkpoint(self):
        payload = json.dumps(list(self.jobs.values()))
        fd, temp = tempfile.mkstemp(dir=self.journal.parent, prefix=".jobs-")
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.replace(temp, self.journal)

    def job_metrics(self, job):
        now = job.get("finished", time.time())
        elapsed = max(0, now - job.get("started", now))
        speed = max(0, job["bytes"] - job.get("initial_bytes", 0)) / elapsed if elapsed else 0
        job.update(
            elapsed=round(elapsed),
            speed=round(speed),
            eta=round((job["total"] - job["bytes"]) / speed)
            if speed > 0 and job["total"] > job["bytes"]
            else None,
        )
        return dict(job)

    async def volumes(self):
        volumes = [
            {
                "id": "o2",
                "name": "O2 Cloud",
                "provider": "o2",
                "connected": self.sessions.read() is not None,
                "can_delete": True,
                "can_move": True,
                "notice": "",
            }
        ]
        for remote in await self.rclone.remotes():
            name, kind = remote["name"].rstrip(":"), remote["type"]
            key = "rclone:" + name
            if key not in self.backends:
                self.backends[key] = RcloneBackend(self.rclone, name, kind)
            volumes.append(
                {
                    "id": key,
                    "name": name,
                    "provider": kind,
                    "connected": True,
                    "can_delete": kind in ("drive", "onedrive"),
                    "can_move": kind != "google photos",
                    "notice": "Solo fotos creadas mediante esta integración. No permite borrado; las descargas pueden perder resolución o EXIF."
                    if kind == "google photos"
                    else "",
                }
            )
        return volumes

    async def select(self, volume):
        if volume not in self.backends:
            await self.volumes()
        if volume not in self.backends:
            raise BridgeError("UNKNOWN_VOLUME", "No se encuentra la cuenta de nube.")
        return self.backends[volume]

    def status(self):
        configured = self.sessions.read() is not None
        expired = bool(self.backend.api.session_expired_at())
        return {
            "configured": configured,
            "expired": expired,
            "auth": self.auth,
            "backend": "gateway-sapi",
            "version": "0.1.0",
        }

    async def login(self):
        if self.login_task and not self.login_task.done():
            return self.auth
        self.auth = {"state": "connecting", "error": None}

        async def run():
            try:
                async with self.lock:
                    if self.sessions.read():
                        try:
                            await self.backend.api.root_folder()
                            self.auth = {"state": "connected", "error": None}
                            return
                        except (CloudError, httpx.HTTPError):
                            pass
                from .login import browser_session

                session = await browser_session(self.backend.api)
                async with self.lock:
                    old = self.sessions.read()
                    self.sessions.save(session)
                    try:
                        await self.backend.reset()
                        await self.backend.api.root_folder()
                    except Exception:
                        if old:
                            self.sessions.save(old)
                        else:
                            self.sessions.delete()
                        raise
                    self.auth = {"state": "connected", "error": None}
            except Exception as error:
                self.auth = {"state": "failed", "error": safe_error(error)}
                log.warning("login failed type=%s", type(error).__name__)

        self.login_task = asyncio.create_task(run())
        return self.auth

    async def logout(self):
        if self.login_task and not self.login_task.done():
            raise BridgeError("LOGIN_ACTIVE", "Cierra la ventana de acceso antes de desconectar.")
        for task in list(self.tasks.values()):
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        async with self.lock:
            self.sessions.delete()
            await self.backend.reset()
            self.jobs.clear()
            self.checkpoint()
            self.auth = {"state": "idle", "error": None}
        return {"disconnected": True}

    def new_job(self, kind, remote, local):
        if len(self.tasks) >= 10:
            raise BridgeError("BUSY", "Espera a que termine alguna transferencia.")
        if len(self.jobs) >= 100:
            completed = [
                k
                for k, v in self.jobs.items()
                if v["state"] in ("completed", "failed", "cancelled")
            ]
            for key in completed[:50]:
                del self.jobs[key]
        job = {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "remote": remote,
            "local": str(local),
            "state": "queued",
            "bytes": 0,
            "total": 0,
            "progress": 0,
            "error": None,
            "result": None,
        }
        job.update(started=time.time(), speed=0, eta=None, elapsed=0, resumable=kind == "download")
        self.jobs[job["id"]] = job
        return job

    def start_transfer(self, kind, remote, local, backend=None, volume="o2", resume=None):
        backend = backend or self.backend
        remote = remote_path(remote, mutable=kind == "upload")
        local = local_path(local, source=kind == "upload")
        job = resume or self.new_job(kind, remote, local)
        job["volume"] = volume
        job.update(started=time.time(), state="queued", error=None)
        job.pop("finished", None)
        job["can_pause"] = kind == "download" or isinstance(backend, RcloneBackend)
        control = Control()
        self.controls[job["id"]] = control
        self.checkpoint()

        async def run():
            temporary = None
            started = time.monotonic()
            control_token = transfer_control.set(control)
            try:
                async with self.transfer_lock:
                    await control.gate.wait()
                    job["state"] = "transferring"
                    if kind == "upload":
                        job["total"] = local.stat().st_size

                        def progress(done, total):
                            fraction = min(done / total, 1) if total else 0
                            job["bytes"] = int(fraction * job["total"])
                            job["progress"] = round(fraction * 99, 1)
                            if total and done >= total:
                                job["state"] = "processing"

                        token = transfer_progress.set(progress)
                        try:
                            item = await backend.upload(remote, str(local), overwrite=False)
                        finally:
                            transfer_progress.reset(token)
                        if item.id.startswith("pending:"):
                            raise BridgeError(
                                "UPLOAD_UNCONFIRMED",
                                "O2 ha recibido la subida pero todavía no ha confirmado su ID. Actualiza el listado antes de repetirla.",
                            )
                        job["result"] = metadata(item)
                    else:
                        item = await read_retry(lambda: backend.get_metadata(remote))
                        if item is None or item.is_folder:
                            raise BridgeError("NOT_A_FILE", "Selecciona un archivo para descargar.")
                        job["total"] = item.size or 0
                        identity = [
                            item.id,
                            item.etag,
                            item.size,
                            item.modified_at.isoformat() if item.modified_at else None,
                        ]
                        if resume and job.get("partial"):
                            temporary = Path(job["partial"])
                            if (
                                temporary.parent.resolve() != local.parent
                                or not temporary.name.startswith(".clouddesk-part-")
                                or not temporary.is_file()
                                or temporary.is_symlink()
                            ):
                                raise BridgeError(
                                    "INVALID_PARTIAL", "No se encuentra un parcial válido."
                                )
                            if identity != job.get("identity") or (
                                not item.etag and not item.modified_at
                            ):
                                raise BridgeError(
                                    "SOURCE_CHANGED",
                                    "El origen cambió o no tiene metadata para reanudar de forma segura.",
                                )
                            offset = temporary.stat().st_size
                            if item.size is None or offset > item.size:
                                raise BridgeError(
                                    "INVALID_PARTIAL",
                                    "No se puede verificar el tamaño del parcial.",
                                )
                        else:
                            fd, temp = tempfile.mkstemp(prefix=".clouddesk-part-", dir=local.parent)
                            os.close(fd)
                            temporary, offset = Path(temp), 0
                        job.update(
                            partial=str(temporary),
                            identity=identity,
                            bytes=offset,
                            initial_bytes=offset,
                        )
                        self.checkpoint()
                        with temporary.open("ab") as handle:
                            stream = (
                                open_read_retry(backend, remote, (offset, None))
                                if offset
                                else open_read_retry(backend, remote)
                            )
                            async for chunk in stream:
                                await control.gate.wait()
                                handle.write(chunk)
                                job["bytes"] += len(chunk)
                                job["progress"] = (
                                    min(99, round(100 * job["bytes"] / job["total"], 1))
                                    if job["total"]
                                    else 0
                                )
                                self.job_metrics(job)
                            handle.flush()
                            os.fsync(handle.fileno())
                        if item.size is not None and job["bytes"] != item.size:
                            raise BridgeError(
                                "SIZE_MISMATCH",
                                "La descarga está incompleta. El archivo de destino no se ha creado.",
                            )
                        # Atomic creation with no overwrite, including a race after initial checks.
                        os.link(temporary, local)
                        temporary.unlink()
                        temporary = None
                        job.pop("partial", None)
                        job["result"] = {"local": str(local), "bytes": job["bytes"]}
                    job.update(state="completed", progress=100, bytes=job["total"] or job["bytes"])
            except asyncio.CancelledError:
                job["state"] = "cancelled"
                job["error"] = {
                    "code": "CANCELLED",
                    "message": "Transferencia cancelada. Una subida puede haber llegado a O2; comprueba el listado.",
                }
            except Exception as error:
                job.update(state="failed", error=safe_error(error))
            finally:
                transfer_control.reset(control_token)
                # Failed/cancelled downloads retain their private partial for an explicit resume.
                self.tasks.pop(job["id"], None)
                self.controls.pop(job["id"], None)
                job["finished"] = time.time()
                self.job_metrics(job)
                self.checkpoint()
                log.info(
                    "transfer kind=%s state=%s bytes=%s ms=%s",
                    kind,
                    job["state"],
                    job["bytes"],
                    round(1000 * (time.monotonic() - started)),
                )

        self.tasks[job["id"]] = asyncio.create_task(run())
        return dict(job)

    async def cross_transfer(self, args):
        source_volume = args.get("source_volume", "o2")
        destination_volume = args.get("destination_volume", "o2")
        source = remote_path(args.get("source", ""), mutable=True)
        destination = remote_path(args.get("destination", ""), mutable=True)
        move = args.get("move", False)
        source_backend, target_backend = (
            await self.select(source_volume),
            await self.select(destination_volume),
        )
        if source_volume == destination_volume and (
            source == destination or destination.startswith(source + "/")
        ):
            raise BridgeError("INVALID_MOVE", "El destino no puede estar dentro del origen.")
        job = self.new_job("move" if move else "copy", source, destination)
        job.update(volume=source_volume, destination_volume=destination_volume)
        job["can_pause"] = False

        async def run():
            try:
                async with self.transfer_lock:
                    item = await read_retry(lambda: source_backend.get_metadata(source))
                    if item is None:
                        raise BridgeError("NOT_FOUND", "No se encuentra el origen.")
                    if move and source_volume == destination_volume:
                        job["state"] = "moving"
                        job["result"] = metadata(
                            await source_backend.move(source, destination, overwrite=False)
                        )
                    else:
                        if move and (
                            item.is_folder
                            or getattr(source_backend, "kind", "") == "google photos"
                            or getattr(target_backend, "kind", "") == "google photos"
                        ):
                            raise BridgeError(
                                "UNSUPPORTED_MOVE",
                                "Mover entre nubes está disponible para archivos de Drive, OneDrive y O2. Para carpetas o Google Fotos, usa Copiar.",
                            )
                        if (
                            move
                            and isinstance(source_backend, RcloneBackend)
                            and source_backend.kind not in ("drive", "onedrive")
                        ):
                            raise BridgeError(
                                "UNSUPPORTED_MOVE",
                                "El origen no garantiza borrado recuperable. Usa Copiar.",
                            )
                        if await target_backend.get_metadata(destination):
                            raise BridgeError("CONFLICT", "El destino ya existe.")
                        plan = []

                        async def collect(entry, dst):
                            if len(plan) >= 5000:
                                raise BridgeError(
                                    "TRANSFER_LIMIT",
                                    "Divide la transferencia en carpetas de menos de 5000 elementos.",
                                )
                            plan.append((entry, dst))
                            if entry.is_folder:
                                for child in await source_backend.list(entry.path):
                                    await collect(child, dst + "/" + child.name)

                        await collect(item, destination)
                        job["total"] = sum(
                            entry.size or 0 for entry, _ in plan if not entry.is_folder
                        )
                        completed = 0
                        for entry, dst in plan:
                            if entry.is_folder:
                                await target_backend.create_folder(dst)
                                continue
                            job["state"] = "downloading"
                            with tempfile.TemporaryDirectory(
                                prefix="cloud-transfer-", dir=state_dir()
                            ) as folder:
                                staged = Path(folder) / entry.name
                                digest = hashlib.sha256()
                                count = 0
                                with staged.open("wb") as handle:
                                    staged.chmod(0o600)
                                    async for chunk in open_read_retry(source_backend, entry.path):
                                        handle.write(chunk)
                                        digest.update(chunk)
                                        count += len(chunk)
                                        job["bytes"] = completed + count
                                        job["progress"] = min(
                                            90,
                                            round(
                                                100
                                                * (completed + count * 0.3)
                                                / (job["total"] or count or 1),
                                                1,
                                            ),
                                        )
                                if entry.size is not None and count != entry.size:
                                    raise BridgeError(
                                        "SIZE_MISMATCH",
                                        "La lectura del origen está incompleta. El origen se conserva.",
                                    )
                                job["state"] = "uploading"

                                def progress(done, total, completed=completed, count=count):
                                    fraction = min(1, done / total) if total else 0
                                    job["progress"] = min(
                                        95,
                                        round(
                                            100
                                            * (completed + count * (0.3 + fraction * 0.5))
                                            / (job["total"] or count or 1),
                                            1,
                                        ),
                                    )

                                token = transfer_progress.set(progress)
                                try:
                                    uploaded = await target_backend.upload(
                                        dst, str(staged), overwrite=False
                                    )
                                finally:
                                    transfer_progress.reset(token)
                                if uploaded.id.startswith("pending:"):
                                    raise BridgeError(
                                        "UNCONFIRMED",
                                        "El destino todavía no ha confirmado la subida. El origen se conserva.",
                                    )
                                # A move deletes its source only after reading back and hashing the destination.
                                # Google Photos may recompress media, so it is always copy-only.
                                if move:
                                    job["state"] = "verifying"
                                    verified = hashlib.sha256()
                                    async for chunk in open_read_retry(target_backend, dst):
                                        verified.update(chunk)
                                    current = hashlib.sha256()
                                    async for chunk in open_read_retry(source_backend, entry.path):
                                        current.update(chunk)
                                    if (
                                        verified.digest() != digest.digest()
                                        or current.digest() != digest.digest()
                                    ):
                                        raise BridgeError(
                                            "CHECKSUM_MISMATCH",
                                            "La verificación no coincide o el origen ha cambiado. El origen se conserva.",
                                        )
                                    await source_backend.delete(entry.path, soft_delete=True)
                                completed += count
                                job["bytes"] = completed
                        job["result"] = {
                            "source": source,
                            "destination": destination,
                            "moved": move,
                            "items": len(plan),
                        }
                    job.update(state="completed", progress=100)
            except asyncio.CancelledError:
                job.update(
                    state="cancelled",
                    error={
                        "code": "CANCELLED",
                        "message": "Operación cancelada. Revisa los elementos ya copiados.",
                    },
                )
            except Exception as error:
                job.update(state="failed", error=safe_error(error))
            finally:
                self.tasks.pop(job["id"], None)
                job["finished"] = time.time()
                self.job_metrics(job)
                self.checkpoint()
                log.info("cloud transfer state=%s bytes=%s", job["state"], job["bytes"])

        self.tasks[job["id"]] = asyncio.create_task(run())
        return dict(job)

    async def rpc(self, operation: str, args: dict):
        if operation == "volumes":
            return await self.volumes()
        if operation == "configure":
            return await asyncio.to_thread(self.rclone.configure)
        if operation == "copy":
            return await self.cross_transfer(args)
        if operation == "status":
            return self.status()
        if operation == "login":
            return await self.login()
        if operation == "logout":
            return await self.logout()
        if operation == "jobs":
            return [self.job_metrics(j) if j["id"] in self.tasks else j for j in self.jobs.values()]
        if operation == "job":
            if args.get("id") not in self.jobs:
                raise BridgeError("JOB_NOT_FOUND", "No se encuentra la transferencia.")
            return self.jobs[args["id"]]
        if operation in ("pause", "resume"):
            job = self.jobs.get(args.get("id"))
            if job is None:
                raise BridgeError("JOB_NOT_FOUND", "No se encuentra la transferencia.")
            control = self.controls.get(job["id"])
            if operation == "pause":
                if not job.get("can_pause") or control is None:
                    raise BridgeError(
                        "PAUSE_UNSUPPORTED",
                        "Esta transferencia no admite pausa segura. Puedes cancelarla y comprobar el destino.",
                    )
                control.pause(job)
            elif control:
                control.resume(job)
            elif (
                job.get("resumable")
                and job.get("partial")
                and job["state"] in ("failed", "cancelled", "interrupted")
            ):
                backend = await self.select(job.get("volume", "o2"))
                return self.start_transfer(
                    "download",
                    job["remote"],
                    job["local"],
                    backend,
                    job.get("volume", "o2"),
                    resume=job,
                )
            else:
                raise BridgeError(
                    "RESUME_UNSUPPORTED",
                    "Esta transferencia no tiene un parcial que se pueda reanudar.",
                )
            self.checkpoint()
            return dict(job)
        if operation == "cancel":
            task = self.tasks.get(args.get("id"))
            if task:
                job = self.jobs[args["id"]]
                control = self.controls.get(args["id"])
                if control and job["state"] == "paused":
                    control.resume(job)
                task.cancel()
            return {"requested": bool(task)}
        if operation in ("upload", "download"):
            backend = await self.select(args.get("volume", "o2"))
            return self.start_transfer(
                operation,
                args.get("remote", ""),
                args.get("local", ""),
                backend,
                args.get("volume", "o2"),
            )
        backend = await self.select(args.get("volume", "o2"))
        if operation == "preview":
            remote = remote_path(args.get("path", ""))
            item = await read_retry(lambda: backend.get_metadata(remote))
            if item is None or item.is_folder or (item.size or 0) > 100 * 1024 * 1024:
                raise BridgeError(
                    "PREVIEW_LIMIT", "La vista previa admite archivos de hasta 100 MB."
                )
            folder = state_dir() / "previews"
            folder.mkdir(mode=0o700, exist_ok=True)
            # Keep a bounded preview cache and never reuse an existing target.
            for old in folder.iterdir():
                if old.is_file() and time.time() - old.stat().st_mtime > 3600:
                    old.unlink()
            local = folder / (uuid.uuid4().hex + "-" + Path(remote).name)
            return self.start_transfer(
                "download", remote, str(local), backend, args.get("volume", "o2")
            )
        async with self.lock:
            if operation == "quota":
                return await read_retry(backend.quota)
            path = remote_path(
                args.get("path", "/"), mutable=operation in ("mkdir", "delete", "move")
            )
            if operation == "list":
                return [metadata(item) for item in await read_retry(lambda: backend.list(path))]
            if operation == "stat":
                item = await read_retry(lambda: backend.get_metadata(path))
                return metadata(item) if item else None
            if operation == "mkdir":
                return metadata(await backend.create_folder(path))
            if operation == "move":
                destination = remote_path(args.get("destination", ""), mutable=True)
                if destination == path or destination.startswith(path + "/"):
                    raise BridgeError(
                        "INVALID_MOVE",
                        "El destino no puede ser el origen ni una carpeta dentro de él.",
                    )
                return metadata(await backend.move(path, destination, overwrite=False))
            if operation == "delete":
                await backend.delete(path, soft_delete=True)
                return {"trashed": True, "path": path}
        raise BridgeError("UNKNOWN_OPERATION", "Operación desconocida.")


def create_app(hub=None, token=None):
    @asynccontextmanager
    async def lifespan(app):
        if hub is None:
            vault = Vault()
            app.state.hub = Hub(GatewayBackend(SessionStore(vault)), SessionStore(vault), vault)
            app.state.token = vault.local_token()
        else:
            app.state.hub, app.state.token = hub, token
        yield
        for task in list(app.state.hub.tasks.values()):
            task.cancel()
        await asyncio.gather(*app.state.hub.tasks.values(), return_exceptions=True)
        await app.state.hub.backend.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    if hub is not None:
        app.state.hub, app.state.token = hub, token

    @app.get("/health")
    async def health():
        return {"service": "o2cloud-desktop", "version": "0.1.0"}

    @app.post("/rpc")
    async def rpc(request: Request):
        if request.headers.get("origin") or not hmac.compare_digest(
            request.headers.get("authorization", ""), "Bearer " + app.state.token
        ):
            return JSONResponse(
                {"error": {"code": "UNAUTHORIZED", "message": "Acceso local no autorizado."}},
                status_code=401,
            )
        try:
            if int(request.headers.get("content-length", "0")) > 16384:
                raise BridgeError("REQUEST_TOO_LARGE", "Petición demasiado grande.")
            raw = await request.body()
            if len(raw) > 16384:
                raise BridgeError("REQUEST_TOO_LARGE", "Petición demasiado grande.")
            body = json.loads(raw)
            if not isinstance(body, dict) or not isinstance(body.get("args", {}), dict):
                raise BridgeError("INVALID_REQUEST", "Petición inválida.")
            result = await app.state.hub.rpc(body.get("operation", ""), body.get("args", {}))
            return {"result": result}
        except Exception as error:
            safe = safe_error(error)
            log.warning("rpc failed code=%s type=%s", safe["code"], type(error).__name__)
            return JSONResponse({"error": safe}, status_code=400)

    return app
