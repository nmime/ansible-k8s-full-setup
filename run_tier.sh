#!/usr/bin/env bash

set -uo pipefail

TIER=${1:?"Usage: $0 <minimal|small|medium|production>"}
PROJECT="k8s-${TIER}"
DOMAIN="${TIER}.n0xeid.xyz"
EMAIL="admin@n0xeid.xyz"
LOGFILE="/root/run-${TIER}.log"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/scripts/load-project-env.sh"

echo "=== Starting $TIER at $(date) ===" | tee "$LOGFILE"
: "${HCLOUD_TOKEN:?Set HCLOUD_TOKEN directly or in ${SCRIPT_DIR}/.env}"
export HCLOUD_TOKEN

cd "$SCRIPT_DIR"
set +e
ansible-playbook playbooks/deploy_platform.yml \
  -e tier="$TIER" \
  -e domain="$DOMAIN" \
  -e email="$EMAIL" \
  -e project_name="$PROJECT" \
  -e hcloud_token="${HCLOUD_TOKEN}" \
  -v 2>&1 | tee -a "$LOGFILE"
RETCODE=${PIPESTATUS[0]}
set -e

if [[ "$RETCODE" -eq 0 ]]; then
  echo "=== $TIER SUCCEEDED at $(date) ===" | tee -a "$LOGFILE"
else
  echo "=== $TIER FAILED (rc=$RETCODE) at $(date) ===" | tee -a "$LOGFILE"
fi

echo "=== Done $TIER at $(date) ===" | tee -a "$LOGFILE"
exit "$RETCODE"
