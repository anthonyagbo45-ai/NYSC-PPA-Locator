#!/usr/bin/env bash
set -euo pipefail
ppa_project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ppa_python="${PPA_PYTHON:-python}"
cd "$ppa_project_dir"
case "${PPA_INITIALIZE_LOCATIONS:-False}" in
    [Tt][Rr][Uu][Ee]) ppa_initialize_locations=1 ;;
    [Ff][Aa][Ll][Ss][Ee]) ppa_initialize_locations=0 ;;
    *) echo 'PPA_INITIALIZE_LOCATIONS must be True or False.' >&2; exit 1 ;;
esac
"$ppa_python" manage.py migrate --noinput
if [ "$ppa_initialize_locations" = 1 ]; then
    "$ppa_python" manage.py initialize_locations
fi
"$ppa_python" manage.py bootstrap_roles
"$ppa_python" manage.py bootstrap_initial_admin
"$ppa_python" manage.py check --deploy
