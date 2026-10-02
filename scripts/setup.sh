#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_dir"
umask 077
./scripts/fetch-rclone.sh
uv sync --frozen
npm ci
.venv/bin/python -m playwright install chromium
sha256sum --check tools/rclone.sha256
npm run tauri -- build --debug --no-bundle -- --jobs 2

mkdir -p bin
cp src-tauri/target/debug/o2cloud-desktop bin/clouddesk
if command -v strip >/dev/null; then strip --strip-debug bin/clouddesk; fi
