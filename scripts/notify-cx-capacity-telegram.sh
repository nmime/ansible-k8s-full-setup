#!/usr/bin/env bash

# Check the complete medium-optimized CX mapping in every EU Hetzner location
# and notify Telegram only when a location becomes newly deployable.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/load-project-env.sh"

PYTHON=python3
[[ ! -x "${PROJECT_ROOT}/.venv/bin/python3" ]] || PYTHON="${PROJECT_ROOT}/.venv/bin/python3"
exec "$PYTHON" "${SCRIPT_DIR}/notify-cx-capacity-telegram.py" "$@"
