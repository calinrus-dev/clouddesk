#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
if [[ "$(uname -s)" != Linux || "$(uname -m)" != x86_64 ]]; then
  echo 'Este instalador requiere Linux x86_64. Consulta tools/README.md para otras plataformas.' >&2
  exit 1
fi
if [[ -f tools/rclone ]]; then
  sha256sum --check tools/rclone.sha256
  exit 0
fi
temporary_dir="$(mktemp -d)"
trap 'rm -rf -- "$temporary_dir"' EXIT
archive=rclone-v1.75.1-linux-amd64.zip
curl --fail --location --proto '=https' --tlsv1.2 \
  "https://downloads.rclone.org/v1.75.1/$archive" -o "$temporary_dir/$archive"
printf '%s  %s\n' \
  '982b5aa772841168f8e380f139e9e787b2a105403e32b94da8676a0e1c0a13ab' \
  "$temporary_dir/$archive" | sha256sum --check -
unzip -q "$temporary_dir/$archive" -d "$temporary_dir"
sha256sum "$temporary_dir/rclone-v1.75.1-linux-amd64/rclone" | \
  cut -d ' ' -f 1 | cmp - <(cut -d ' ' -f 1 tools/rclone.sha256)
install -m 755 "$temporary_dir/rclone-v1.75.1-linux-amd64/rclone" tools/rclone
sha256sum --check tools/rclone.sha256
