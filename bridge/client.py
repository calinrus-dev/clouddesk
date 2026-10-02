from __future__ import annotations

import asyncio
import subprocess
import sys
import time

import httpx
from filelock import FileLock

from .security import PORT, ROOT, BridgeError, Vault, state_dir

URL = f"http://127.0.0.1:{PORT}"


def credentials():
    """Returned to the native Rust process over a pipe, never written to files."""
    with FileLock(str(state_dir() / "start.lock"), timeout=20):
        token = Vault().local_token()

        def running():
            try:
                response = httpx.get(URL + "/health", timeout=0.5, trust_env=False)
                return (
                    response.status_code == 200
                    and response.json().get("service") == "o2cloud-desktop"
                )
            except (httpx.HTTPError, ValueError):
                return False

        if not running():
            subprocess.Popen(
                [sys.executable, "-m", "bridge", "serve"],
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            for _ in range(100):
                if running():
                    break
                time.sleep(0.1)
            else:
                raise BridgeError(
                    "SERVICE_START_FAILED",
                    "No se pudo iniciar el servicio local. Revisa el llavero y el puerto 47823.",
                )
        return {"url": URL, "token": token}


class Client:
    def __init__(self):
        self.auth = None

    async def call(self, operation, **args):
        if self.auth is None:
            self.auth = await asyncio.to_thread(credentials)
        async with httpx.AsyncClient(timeout=330, trust_env=False) as client:
            try:
                response = await client.post(
                    self.auth["url"] + "/rpc",
                    json={"operation": operation, "args": args},
                    headers={"Authorization": "Bearer " + self.auth["token"]},
                )
            except httpx.HTTPError:
                self.auth = None
                raise BridgeError(
                    "LOCAL_SERVICE_OFFLINE",
                    "El servicio local no responde. Repite la consulta; no repitas escrituras sin comprobarlas.",
                ) from None
            body = response.json()
            if "error" in body:
                raise BridgeError(body["error"]["code"], body["error"]["message"])
            return body["result"]
