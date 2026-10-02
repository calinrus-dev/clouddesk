from __future__ import annotations

import asyncio

from mcp.server.fastmcp import Context, FastMCP
from mcp.types import ToolAnnotations

from .client import Client
from .security import BridgeError

client = Client()
mcp = FastMCP(
    "CloudDesk",
    instructions="Gestiona las cuentas de nube con cloud_accounts y las herramientas cloud_*. O2 usa el adaptador SAPI, las demás cuentas rclone. Google Fotos solo expone contenido creado mediante esta integración y no permite borrarlo. Gestiona O2 Cloud mediante el servicio local compartido. Usa rutas absolutas de O2 (/Carpeta/archivo). Las eliminaciones van a la papelera. No hay borrado permanente ni sobrescritura. Ante una escritura fallida, consulta el listado antes de repetirla. La sesión se guarda en el llavero del escritorio. Si caduca, el usuario pulsa Conectar O2 en la aplicación gráfica. Las transferencias reportan progreso y pueden tardar. No solicites ni muestres cookies, contraseñas o tokens.",
)
READ = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=True
)
WRITE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True
)
DELETE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=True, openWorldHint=True
)


async def call(operation, **args):
    try:
        return await client.call(operation, **args)
    except BridgeError as error:
        # Tool failures must be isError, not a successful JSON result containing an error.
        raise ValueError(f"{error.code}: {error.message}") from None


@mcp.tool(annotations=READ)
async def o2_status() -> dict:
    """Estado de la sesión local de O2; no devuelve secretos."""
    return await call("status")


@mcp.tool(annotations=READ)
async def o2_list(path: str = "/") -> list[dict]:
    """Lista archivos y subcarpetas de una carpeta de O2 Cloud."""
    return await call("list", path=path)


@mcp.tool(annotations=READ)
async def o2_stat(path: str) -> dict | None:
    """Consulta tamaño, tipo y fecha de modificación."""
    return await call("stat", path=path)


@mcp.tool(annotations=READ)
async def o2_quota() -> dict:
    """Consulta el espacio ocupado y disponible en O2."""
    return await call("quota")


@mcp.tool(annotations=WRITE)
async def o2_mkdir(path: str) -> dict:
    """Crea una carpeta; su padre debe existir."""
    return await call("mkdir", path=path)


@mcp.tool(annotations=WRITE)
async def o2_move(path: str, destination: str) -> dict:
    """Mueve o renombra un archivo/carpeta sin sobrescribir el destino."""
    return await call("move", path=path, destination=destination)


@mcp.tool(annotations=DELETE)
async def o2_delete(path: str) -> dict:
    """Envía un archivo/carpeta a la papelera de O2."""
    return await call("delete", path=path)


async def transfer(kind, remote, local, ctx):
    job = await call(kind, remote=remote, local=local)
    try:
        while job["state"] not in ("completed", "failed", "cancelled"):
            await ctx.report_progress(
                job["progress"],
                100,
                message=f"{job['state']}: {job['bytes']} / {job['total']} bytes",
            )
            await asyncio.sleep(0.5)
            job = await call("job", id=job["id"])
    except asyncio.CancelledError:
        await client.call("cancel", id=job["id"])
        raise
    if job["error"]:
        raise ValueError(f"{job['error']['code']}: {job['error']['message']}")
    await ctx.report_progress(100, 100, message="Completado")
    return job["result"]


@mcp.tool(annotations=WRITE)
async def o2_upload(local: str, remote: str, ctx: Context) -> dict:
    """Sube un archivo local a O2, con progreso y sin sobrescribir."""
    return await transfer("upload", remote, local, ctx)


@mcp.tool(annotations=WRITE)
async def o2_download(remote: str, local: str, ctx: Context) -> dict:
    """Descarga a una ruta local nueva, con progreso y comprobación de tamaño."""
    return await transfer("download", remote, local, ctx)


@mcp.tool(annotations=READ)
async def o2_transfers() -> list[dict]:
    """Lista las transferencias de todos los clientes del servicio local."""
    return await call("jobs")


def run():
    mcp.run(transport="stdio")


@mcp.tool(annotations=READ)
async def cloud_accounts() -> list[dict]:
    """Lista las cuentas conectadas y sus capacidades; no muestra credenciales."""
    return await call("volumes")


@mcp.tool(annotations=READ)
async def cloud_list(volume: str, path: str = "/") -> list[dict]:
    """Lista una carpeta en O2, Drive, OneDrive u otra cuenta configurada."""
    return await call("list", volume=volume, path=path)


@mcp.tool(annotations=READ)
async def cloud_stat(volume: str, path: str) -> dict | None:
    """Metadata de un archivo o carpeta de una nube."""
    return await call("stat", volume=volume, path=path)


@mcp.tool(annotations=READ)
async def cloud_quota(volume: str) -> dict:
    """Cuota; los valores no disponibles se devuelven como null."""
    return await call("quota", volume=volume)


@mcp.tool(annotations=WRITE)
async def cloud_mkdir(volume: str, path: str) -> dict:
    """Crea una carpeta en una cuenta conectada."""
    return await call("mkdir", volume=volume, path=path)


@mcp.tool(annotations=WRITE)
async def cloud_move(volume: str, path: str, destination: str) -> dict:
    """Mueve o renombra dentro de una nube, sin sobrescribir."""
    return await call("move", volume=volume, path=path, destination=destination)


@mcp.tool(annotations=DELETE)
async def cloud_delete(volume: str, path: str) -> dict:
    """Borrado recuperable en O2, Drive y OneDrive; bloqueado en otros proveedores."""
    return await call("delete", volume=volume, path=path)


@mcp.tool(annotations=WRITE)
async def cloud_upload(volume: str, local: str, remote: str) -> dict:
    """Inicia una subida y devuelve un job. Sigue su progreso con cloud_jobs."""
    return await call("upload", volume=volume, local=local, remote=remote)


@mcp.tool(annotations=WRITE)
async def cloud_download(volume: str, remote: str, local: str) -> dict:
    """Inicia una descarga reanudable a un archivo local nuevo."""
    return await call("download", volume=volume, local=local, remote=remote)


@mcp.tool(annotations=DELETE)
async def cloud_copy(
    source_volume: str, source: str, destination_volume: str, destination: str, move: bool = False
) -> dict:
    """Inicia una copia entre nubes. move=True verifica SHA-256 antes de retirar un archivo del origen. Carpetas y Google Fotos admiten copia."""
    return await call(
        "copy",
        source_volume=source_volume,
        source=source,
        destination_volume=destination_volume,
        destination=destination,
        move=move,
    )


@mcp.tool(annotations=READ)
async def cloud_jobs() -> list[dict]:
    """Procesos compartidos con progreso, velocidad, ETA y errores."""
    return await call("jobs")


@mcp.tool(annotations=WRITE)
async def cloud_pause(job_id: str) -> dict:
    """Pausa una transferencia que anuncie can_pause=True."""
    return await call("pause", id=job_id)


@mcp.tool(annotations=WRITE)
async def cloud_resume(job_id: str) -> dict:
    """Reanuda una pausa o una descarga interrumpida con parcial verificable."""
    return await call("resume", id=job_id)


@mcp.tool(annotations=WRITE)
async def cloud_cancel(job_id: str) -> dict:
    """Cancela un proceso; no deshace los elementos ya copiados."""
    return await call("cancel", id=job_id)
