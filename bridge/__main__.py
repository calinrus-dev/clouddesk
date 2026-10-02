import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from .security import PORT, BridgeError, state_dir


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "mcp"
    if command == "serve":
        import os

        import uvicorn

        from .service import create_app

        os.umask(0o077)
        handler = RotatingFileHandler(state_dir() / "service.log", maxBytes=512_000, backupCount=2)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger = logging.getLogger("o2desktop")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        uvicorn.run(
            create_app(), host="127.0.0.1", port=PORT, log_level="critical", access_log=False
        )
    elif command == "credential":
        from .client import credentials

        try:
            print(json.dumps(credentials()))
        except BridgeError as error:
            print(json.dumps({"error": {"code": error.code, "message": error.message}}))
            sys.exit(1)
    elif command == "mcp":
        from .mcp_server import run

        run()
    elif command == "rclone-password":
        from .security import Vault

        # Private subprocess pipe consumed only by rclone's password helper.
        print(Vault().get("rclone-password") or "")
    elif command == "configure":
        import os
        import subprocess

        from .rclone_backend import Rclone

        os.umask(0o077)
        rclone = Rclone()
        rclone.prepare()
        print("CloudDesk · Añadir cuentas de nube\n")
        print("Elige n para añadir una cuenta. Proveedores: drive, onedrive, google photos.")
        print("La configuración ya está cifrada. No cambies su contraseña desde el asistente.")
        print(
            "Google Fotos solo muestra contenido creado mediante esta integración; no permite borrar fotos.\n"
        )
        subprocess.run([rclone.binary, "--config", str(rclone.config), "config"], env=rclone.env())
        input("\nPulsa Intro para cerrar y vuelve a Actualizar cuentas en CloudDesk.")
    else:
        sys.exit("Usa: python -m bridge serve|mcp|credential")


if __name__ == "__main__":
    main()
