#!/usr/bin/env bash
set -euo pipefail
ppa_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ppa_repo_dir="$(dirname -- "$ppa_project_dir")"
ppa_python="${PPA_PYTHON:-python}"
cd "$ppa_repo_dir"
npm ci --include=dev
npm run build:css
cd "$ppa_project_dir"
"$ppa_python" -m pip install -r requirements.txt
"$ppa_python" manage.py check
"$ppa_python" manage.py collectstatic --noinput
# Apply migrations once through the reviewed release/predeploy command, not builds.
