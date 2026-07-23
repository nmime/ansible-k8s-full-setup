#!/usr/bin/env bash

# Load the protected project environment, then print the authenticated live
# Hetzner catalog and five-profile tariff totals without exposing credentials.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"

# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/load-project-env.sh"

: "${HCLOUD_TOKEN:?Set HCLOUD_TOKEN directly or in ${PROJECT_ROOT}/.env}"
export HCLOUD_TOKEN

PYTHON=python3
[[ ! -x "${PROJECT_ROOT}/.venv/bin/python3" ]] || PYTHON="${PROJECT_ROOT}/.venv/bin/python3"
exec "$PYTHON" "${SCRIPT_DIR}/hetzner-capacity-report.py" "$@"
