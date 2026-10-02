#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export O2_DESKTOP_ROOT="$project_dir"
cd "$project_dir"
if [[ ! -x .venv/bin/python ]]; then echo 'Ejecuta scripts/setup.sh primero.' >&2; exit 1; fi
if [[ -x bin/clouddesk ]]; then exec bin/clouddesk; fi
if [[ -x src-tauri/target/release/o2cloud-desktop ]]; then exec src-tauri/target/release/o2cloud-desktop; fi
if [[ -x src-tauri/target/debug/o2cloud-desktop ]]; then exec src-tauri/target/debug/o2cloud-desktop; fi
exec npm run tauri -- dev
