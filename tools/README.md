# rclone

El repositorio distribuye código fuente, sin binarios ni configuraciones de cuentas.
`scripts/fetch-rclone.sh` descarga rclone **1.75.1 para Linux x86_64** del servidor
oficial y comprueba tanto el ZIP como el binario antes de instalarlo en `tools/rclone`.
El SHA-256 del ZIP se contrastó con el [manifest oficial](https://downloads.rclone.org/v1.75.1/SHA256SUMS).

Para otras arquitecturas hay que adaptar el instalador y el checksum al archivo
oficial correspondiente. No se ha verificado esa portabilidad.

rclone: [repositorio](https://github.com/rclone/rclone), [licencia MIT](https://rclone.org/licence/).
