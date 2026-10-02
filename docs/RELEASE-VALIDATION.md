# Validación de la entrega pública — 2 de octubre de 2026

Comprobaciones ejecutadas sobre esta copia de fuente:

- `uv sync --frozen`: entorno reproducido desde el lockfile.
- `uv run ruff check bridge tests`: sin errores.
- `uv run pytest -q`: **25 pruebas superadas**.
- `npm ci --no-audit --no-fund` y `npm run build`: instalación, TypeScript y Vite correctos.
- `cargo test --manifest-path src-tauri/Cargo.toml --all-targets -j 2`: **una prueba Rust superada**.
- `scripts/fetch-rclone.sh`: descarga oficial 1.75.1 y checksums de ZIP y binario correctos.
- `bash -n scripts/*.sh`: sintaxis correcta.
- Navegador: modo demostración, entrada y regreso de una carpeta; captura real con datos ficticios.
- MCP stdio: inicialización y descubrimiento de 24 herramientas, sin actuar sobre cuentas.
- Revisión del contenido staged con Gitleaks 8.30.1, sin secretos detectados.

Los avisos de build incluyen dependencias npm transitivas obsoletas, Tailwind 2 JIT y un aviso PostCSS; no impidieron compilar. No equivalen a una auditoría de dependencias.

La evidencia de operaciones reales O2 corresponde a la implementación original
del 1 de octubre y se conserva en `VALIDATION.json`. Durante una nueva
comprobación de solo lectura el 2 de octubre, el proveedor pidió renovar la
sesión. No se repitieron escrituras ni se usaron credenciales ajenas. La
reconexión interactiva sigue siendo necesaria cuando la sesión caduca.

Drive, OneDrive y Google Fotos todavía requieren pruebas con cuentas reales.
La entrega no incluye estado, sesiones, configuraciones de cuentas ni binarios
precompilados. Las pruebas no acreditan rendimiento, disponibilidad permanente,
compatibilidad multiplataforma ni una auditoría de seguridad completa.
