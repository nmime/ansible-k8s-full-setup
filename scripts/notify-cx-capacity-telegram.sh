#!/usr/bin/env bash

# Check CX33/CX43 availability in Hetzner Helsinki (hel1), notify Telegram, and
# acquire at most one CX33 plus two CX43 servers when local ordering is enabled.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/load-project-env.sh"

PYTHON=python3
[[ ! -x "${PROJECT_ROOT}/.venv/bin/python3" ]] || PYTHON="${PROJECT_ROOT}/.venv/bin/python3"
exec "$PYTHON" "${SCRIPT_DIR}/notify-cx-capacity-telegram.py" "$@"
