from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from o2gateway.o2.session import O2Cookie, O2Session, deserialize_session, serialize_session

ROOT = Path(__file__).resolve().parent.parent
STATE = Path(
    os.environ.get("O2_DESKTOP_STATE_DIR")
    or str(Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state") / "clouddesk")
)
PORT = 47823
SERVICE = "o2cloud-desktop"


class BridgeError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Vault:
    """Use Secret Service explicitly; never fall back to an unencrypted keyring."""

    def __init__(self, backend=None):
        if backend is None:
            from keyring.backends.SecretService import Keyring

            backend = Keyring()
        self.backend = backend

    def get(self, name: str) -> str | None:
        try:
            return self.backend.get_password(SERVICE, name)
        except Exception:
            raise BridgeError("VAULT_LOCKED", "Desbloquea el llavero del escritorio.") from None

    def put(self, name: str, value: str) -> None:
        try:
            self.backend.set_password(SERVICE, name, value)
        except Exception:
            raise BridgeError(
                "VAULT_LOCKED", "No se puede guardar la sesión en el llavero."
            ) from None

    def delete(self, name: str) -> None:
        if self.get(name) is not None:
            try:
                self.backend.delete_password(SERVICE, name)
            except Exception:
                raise BridgeError(
                    "VAULT_LOCKED", "No se puede borrar la sesión del llavero."
                ) from None

    def local_token(self) -> str:
        token = self.get("local-token")
        if token is None:
            token = secrets.token_urlsafe(48)
            self.put("local-token", token)
        return token


class SessionStore:
    def __init__(self, vault: Vault):
        self.vault = vault

    def read(self) -> O2Session | None:
        raw = self.vault.get("o2-session")
        if raw is None:
            return None
        try:
            session = deserialize_session(json.loads(raw))
        except (ValueError, TypeError, AttributeError):
            raise BridgeError(
                "INVALID_SESSION", "La sesión guardada no es válida. Conecta O2 otra vez."
            ) from None
        return session

    def save(self, session: O2Session) -> None:
        if not session.validation_key or not any(c.name == "JSESSIONID" for c in session.cookies):
            raise BridgeError("INVALID_SESSION", "O2 no ha devuelto una sesión completa.")
        self.vault.put("o2-session", json.dumps(serialize_session(session)))

    def delete(self) -> None:
        self.vault.delete("o2-session")


def session_from_cookie(header: str) -> O2Session:
    cookies = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name in ("JSESSIONID", "validationKey") and value:
            cookies.append(O2Cookie(name, value))
    token = next((c.value for c in cookies if c.name == "validationKey"), "")
    if not token or not any(c.name == "JSESSIONID" for c in cookies):
        raise BridgeError("INVALID_SESSION", "Faltan las cookies de sesión de O2.")
    return O2Session(validation_key=token, cookies=cookies)


def state_dir() -> Path:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    STATE.chmod(0o700)
    return STATE


def local_path(value: str, *, source: bool = False) -> Path:
    path = Path(value).expanduser().resolve()
    defaults = [Path.home() / x for x in ("Documents", "Downloads", "Pictures", "Desktop")]
    defaults += [state_dir() / "previews"]
    extra = [
        Path(p).expanduser().resolve()
        for p in os.environ.get("O2_DESKTOP_LOCAL_ROOTS", "").split(os.pathsep)
        if p
    ]
    if not any(path.is_relative_to(root.resolve()) for root in defaults + extra):
        raise BridgeError(
            "LOCAL_PATH_DENIED",
            "Elige un archivo dentro de Documentos, Descargas, Imágenes o Escritorio.",
        )
    if source and (not path.is_file() or path.is_symlink()):
        raise BridgeError("LOCAL_NOT_FOUND", "No se encuentra el archivo local.")
    if not source and (not path.parent.is_dir() or path.exists()):
        raise BridgeError("LOCAL_CONFLICT", "El destino ya existe o su carpeta no existe.")
    return path


def remote_path(value: str, *, mutable: bool = False) -> str:
    if not value.startswith("/") or "\\" in value or "\x00" in value:
        raise BridgeError("INVALID_PATH", "Usa una ruta de O2 que empiece por /.")
    parts = value.split("/")
    if any(p in (".", "..") for p in parts):
        raise BridgeError("INVALID_PATH", "La ruta no puede incluir . ni ..")
    normalized = "/" + "/".join(p for p in parts if p)
    if mutable and normalized == "/":
        raise BridgeError("ROOT_PROTECTED", "No se puede modificar la raíz de O2.")
    return normalized
