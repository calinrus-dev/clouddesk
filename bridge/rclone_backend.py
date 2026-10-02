from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
from datetime import datetime

from filelock import FileLock
from o2gateway.cloud.base import CloudItemMetadata

from .backend import transfer_control, transfer_progress
from .security import ROOT, BridgeError, Vault, state_dir


class Rclone:
    def __init__(self, vault=None, binary=None, config=None):
        self.vault = vault or Vault()
        self.binary = str(binary or ROOT / "tools/rclone")
        self.config = config or state_dir() / "rclone.conf"

    def env(self):
        # Do not inherit arbitrary RCLONE_* backend options or remote definitions.
        env = {k: v for k, v in os.environ.items() if not k.startswith("RCLONE_")}
        password = self.vault.get("rclone-password")
        if password is None:
            password = secrets.token_urlsafe(48)
            self.vault.put("rclone-password", password)
        env["RCLONE_CONFIG_PASS"] = password
        return env

    def prepare(self):
        with FileLock(str(state_dir() / "rclone-setup.lock")):
            if self.config.exists():
                if b"RCLONE_ENCRYPT_V0:" not in self.config.read_bytes()[:300]:
                    raise BridgeError(
                        "UNENCRYPTED_CONFIG", "Se ha rechazado una configuración rclone sin cifrar."
                    )
                return
            self.config.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.config.write_text("")  # Empty file only, never OAuth tokens.
            self.config.chmod(0o600)
            self.env()  # Generate the password in the keyring before invoking its helper.
            helper = shlex.join([sys.executable, "-m", "bridge", "rclone-password"])
            env = self.env()
            env.pop("RCLONE_CONFIG_PASS", None)
            result = subprocess.run(
                [
                    self.binary,
                    "--config",
                    str(self.config),
                    "--password-command",
                    helper,
                    "config",
                    "encryption",
                    "set",
                ],
                env=env,
                cwd=ROOT,
                capture_output=True,
                timeout=30,
            )
            if result.returncode or b"RCLONE_ENCRYPT_V0:" not in self.config.read_bytes()[:300]:
                self.config.unlink(missing_ok=True)
                raise BridgeError(
                    "RCLONE_ENCRYPTION_FAILED",
                    "No se pudo preparar la configuración cifrada de rclone.",
                )

    async def run(self, *args, progress=False, missing_ok=False):
        await asyncio.to_thread(self.prepare)
        extra = [
            "--use-json-log",
            "--log-level",
            "ERROR",
            "--retries",
            "1",
            "--low-level-retries",
            "2",
        ]
        if progress:
            extra = [
                "--use-json-log",
                "--log-level",
                "INFO",
                "--stats",
                "500ms",
                "--stats-log-level",
                "INFO",
                "--retries",
                "1",
                "--low-level-retries",
                "2",
            ]
        process = await asyncio.create_subprocess_exec(
            self.binary,
            "--config",
            str(self.config),
            *extra,
            *args,
            env=self.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        control = transfer_control.get()
        if progress and control:
            control.process = process
            if not control.gate.is_set():
                os.kill(process.pid, signal.SIGSTOP)

        async def read_stats():
            async for line in process.stderr:
                if progress:
                    try:
                        entry = json.loads(line)
                        stats = entry.get("stats", {})
                        callback = transfer_progress.get()
                        if callback and "bytes" in stats:
                            callback(stats["bytes"], stats.get("totalBytes", 0))
                    except (ValueError, TypeError):
                        pass
                # All rclone logs are consumed and discarded, never saved or returned.

        reader = asyncio.create_task(read_stats())
        try:
            output = await asyncio.wait_for(process.stdout.read(), timeout=3600 if progress else 60)
            await process.wait()
            await reader
        except BaseException:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            raise
        finally:
            if progress and control:
                control.process = None
        if process.returncode:
            if missing_ok and process.returncode in (3, 4):
                return None
            raise BridgeError(
                "RCLONE_FAILED",
                f"rclone no pudo completar la operación (código {process.returncode}). Revisa la cuenta y el destino.",
            )
        return output

    async def remotes(self):
        result = json.loads(await self.run("listremotes", "--json"))
        # The documented listremotes output contains names/types, never tokens.
        if isinstance(result, dict):
            result = result.get("remotes", [])
        return [
            r for r in result if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_. -]*", r.get("name", ""))
        ]

    def configure(self):
        self.prepare()
        from shutil import which

        terminal = which("x-terminal-emulator")
        if not terminal:
            raise BridgeError("NO_TERMINAL", "Ejecuta scripts/configure-clouds.sh en tu terminal.")
        subprocess.Popen(
            [terminal, "-e", sys.executable, "-m", "bridge", "configure"],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"opened": True}


class RcloneBackend:
    def __init__(self, rclone, name, kind):
        self.rclone, self.name, self.kind = rclone, name, kind

    def remote(self, path):
        return self.name + ":" + path.lstrip("/")

    def item(self, raw, parent):
        name = raw["Name"]
        size = raw.get("Size", -1)
        modified = raw.get("ModTime")
        return CloudItemMetadata(
            id=raw.get("ID", raw.get("Path", name)),
            name=name,
            type="folder" if raw["IsDir"] else "file",
            path=parent.rstrip("/") + "/" + name,
            size=None if raw["IsDir"] or size < 0 else size,
            modified_at=datetime.fromisoformat(modified.replace("Z", "+00:00"))
            if modified
            else None,
            content_type=raw.get("MimeType"),
        )

    async def list(self, path):
        result = json.loads(await self.rclone.run("lsjson", self.remote(path)))
        return [self.item(raw, path) for raw in result]

    async def get_metadata(self, path):
        result = await self.rclone.run("lsjson", self.remote(path), "--stat", missing_ok=True)
        if result is None:
            return None
        raw = json.loads(result)
        return self.item(raw, path.rsplit("/", 1)[0])

    async def create_folder(self, path):
        if await self.get_metadata(path):
            raise BridgeError("CONFLICT", "El destino ya existe.")
        await self.rclone.run("mkdir", self.remote(path))
        item = await self.get_metadata(path)
        if item is None:
            raise BridgeError("UNCONFIRMED", "El proveedor no ha confirmado la carpeta.")
        return item

    async def upload(self, path, local, *, overwrite=False):
        if overwrite or await self.get_metadata(path):
            raise BridgeError("CONFLICT", "El destino ya existe o se ha solicitado sobrescribir.")
        if self.kind == "google photos":
            mime = mimetypes.guess_type(local)[0] or ""
            if not mime.startswith(("image/", "video/")):
                raise BridgeError("PHOTOS_ONLY", "Google Fotos solo admite imágenes y vídeos.")
            if not path.startswith(("/upload/", "/album/")):
                raise BridgeError(
                    "PHOTOS_TARGET", "Sube fotos a /upload o a un álbum dentro de /album."
                )
        await self.rclone.run("copyto", local, self.remote(path), "--immutable", progress=True)
        item = await self.get_metadata(path)
        if item is None:
            raise BridgeError(
                "UNCONFIRMED",
                "El proveedor no ha confirmado el archivo. Revisa el listado antes de repetirlo.",
            )
        return item

    async def open_read(self, path, byte_range=None):
        await asyncio.to_thread(self.rclone.prepare)
        extra = ["--offset", str(byte_range[0])] if byte_range else []
        process = await asyncio.create_subprocess_exec(
            self.rclone.binary,
            "--config",
            str(self.rclone.config),
            "cat",
            self.remote(path),
            "--retries",
            "1",
            "--log-level",
            "ERROR",
            *extra,
            env=self.rclone.env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            while chunk := await asyncio.wait_for(process.stdout.read(1024 * 1024), 300):
                yield chunk
            await process.wait()
            if process.returncode:
                raise BridgeError(
                    "RCLONE_DOWNLOAD_FAILED", "La descarga del proveedor no se ha completado."
                )
        finally:
            if process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()

    async def move(self, source, destination, *, overwrite=False):
        if self.kind == "google photos":
            raise BridgeError(
                "UNSUPPORTED", "Google Fotos no permite mover o renombrar como un disco."
            )
        if overwrite or await self.get_metadata(destination):
            raise BridgeError("CONFLICT", "El destino ya existe.")
        await self.rclone.run(
            "moveto", self.remote(source), self.remote(destination), "--immutable"
        )
        item = await self.get_metadata(destination)
        if item is None:
            raise BridgeError("UNCONFIRMED", "El proveedor no ha confirmado el movimiento.")
        return item

    async def delete(self, path, *, soft_delete=True):
        if not soft_delete or self.kind not in ("drive", "onedrive"):
            raise BridgeError(
                "UNSUPPORTED", "Esta cuenta no ofrece borrado recuperable desde la aplicación."
            )
        item = await self.get_metadata(path)
        if item is None:
            return
        # Drive's default is trash; OneDrive's Delete uses the recycle bin.
        if item.is_folder:
            await self.rclone.run("delete", self.remote(path))
            await self.rclone.run("rmdirs", self.remote(path))
        else:
            await self.rclone.run("deletefile", self.remote(path))

    async def quota(self):
        if self.kind == "google photos":
            return {"used_bytes": None, "total_bytes": None, "free_bytes": None}
        raw = json.loads(await self.rclone.run("about", self.remote("/"), "--json"))
        return {
            "used_bytes": raw.get("used"),
            "total_bytes": raw.get("total"),
            "free_bytes": raw.get("free"),
        }

    async def close(self):
        pass
