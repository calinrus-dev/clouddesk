# Dependencias y atribuciones

CloudDesk reutiliza software comunitario en lugar de duplicar el protocolo O2.

| Proyecto | Uso | Referencia |
|---|---|---|
| o2cloud_gateway_webdav | Cliente SAPI, modelos y helpers de OAuth | MIT; commit `995756d4f36e97981f27045fc631b509cf8612b9`; licencia conservada en `docs/LICENSE-o2gateway.txt` |
| rclone | Proveedores adicionales y transferencias | MIT; 1.75.1; descargado por separado del servidor oficial |
| o2cloud-cli | Referencia durante investigación | No se redistribuye ni se ejecuta como backend |
| o2cloud 0.4.0 | Evaluación de autenticación | No forma parte del backend final |

Las dependencias Python, npm y Rust se resuelven desde `uv.lock`,
`package-lock.json` y `src-tauri/Cargo.lock`; cada una conserva su licencia.
La licencia MIT de CloudDesk cubre su código propio y no reemplaza las licencias
de terceros ni las condiciones de uso de los proveedores de nube.
