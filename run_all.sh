#!/bin/bash
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/scripts/load-project-env.sh"

echo "=== FULL STACK 3x RUN STARTED at $(date) ==="
RESULTS=""

for TIER in minimal medium production; do
  echo ""
  echo "============================================"
  echo "  TIER: $TIER - Starting at $(date)"
  echo "============================================"
  if bash "${SCRIPT_DIR}/run_tier.sh" "$TIER"; then
    RESULTS="$RESULTS\n$TIER: PASS"
  else
    RESULTS="$RESULTS\n$TIER: FAIL"
  fi
  sleep 10
done

echo ""
echo "============================================"
echo "  RESULTS SUMMARY"
echo "============================================"
echo -e "$RESULTS"
echo "============================================"
echo "Logs: /root/run-minimal.log /root/run-medium.log /root/run-production.log"
echo "=== ALL COMPLETE at $(date) ==="
