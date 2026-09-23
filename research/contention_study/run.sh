#!/usr/bin/env bash
# Thin launcher: puts the study package and the read-only kernel on PYTHONPATH
# and runs a mining module inside the isolated research venv.
#
#   ./run.sh mining.fetch_prs django/django
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPATH="${here}:${here}/../.."
exec "${here}/.venv/bin/python" -m "$@"
