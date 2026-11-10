#!/usr/bin/env bash
set -euo pipefail
ppa_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ppa_python="${PPA_PYTHON:-python}"
cd "$ppa_project_dir"
"$ppa_python" manage.py migrate --noinput
"$ppa_python" manage.py bootstrap_roles
"$ppa_python" manage.py check --deploy
