#!/usr/bin/env bash
# Destructive Kubespray reset/redeploy cycle test for an explicitly named lab.
set -euo pipefail

: "${KUBESPRAY_DIR:?Set KUBESPRAY_DIR to the checked-out Kubespray directory}"
: "${BASTION_HOST:?Set BASTION_HOST for SSH verification}"
: "${CONTROL_HOST:?Set CONTROL_HOST for SSH verification}"

INVENTORY="${INVENTORY:-${KUBESPRAY_DIR}/inventory/k8s/hosts.yml}"
LOG_DIR="${LOG_DIR:-/tmp/kubespray-cycle-tests}"
TOTAL_CYCLES="${TOTAL_CYCLES:-3}"
SSH_USER="${SSH_USER:-root}"
EXPECTED_CONFIRMATION="RESET_AND_REDEPLOY_${TOTAL_CYCLES}_TIMES"

if [[ "${CYCLE_TEST_CONFIRM:-}" != "$EXPECTED_CONFIRMATION" ]]; then
  echo "Refusing destructive cycle test." >&2
  echo "Set CYCLE_TEST_CONFIRM=$EXPECTED_CONFIRMATION after reviewing the targets." >&2
  exit 2
fi
[[ "$TOTAL_CYCLES" =~ ^[1-9][0-9]*$ ]] || { echo "TOTAL_CYCLES must be a positive integer" >&2; exit 2; }
[[ -d "$KUBESPRAY_DIR" ]] || { echo "Kubespray directory not found: $KUBESPRAY_DIR" >&2; exit 2; }
[[ -f "$INVENTORY" ]] || { echo "Inventory not found: $INVENTORY" >&2; exit 2; }
command -v ansible-playbook >/dev/null || { echo "ansible-playbook is required" >&2; exit 2; }
command -v ssh >/dev/null || { echo "ssh is required" >&2; exit 2; }

mkdir -p "$LOG_DIR"

remote_kubectl() {
  ssh -o StrictHostKeyChecking=accept-new \
    -J "${SSH_USER}@${BASTION_HOST}" "${SSH_USER}@${CONTROL_HOST}" "$@"
}

verify_cluster() {
  local attempt=0 max_attempts=30 not_running
  while [[ "$attempt" -lt "$max_attempts" ]]; do
    if remote_kubectl 'kubectl get nodes' 2>/dev/null | grep -q Ready; then
      remote_kubectl 'kubectl get nodes -o wide; kubectl get pods -A' 2>/dev/null
      not_running=$(remote_kubectl \
        "kubectl get pods -A --no-headers | awk '\$4 != \"Running\" && \$4 != \"Completed\" {count++} END {print count+0}'")
      if [[ "$not_running" -eq 0 ]]; then
        echo "All nodes and pods are healthy"
        return 0
      fi
      echo "Waiting for pods: $not_running not ready"
    fi
    attempt=$((attempt + 1))
    sleep 10
  done
  echo "Cluster verification failed after $max_attempts attempts" >&2
  return 1
}

for ((cycle = 1; cycle <= TOTAL_CYCLES; cycle++)); do
  echo "Cycle $cycle of $TOTAL_CYCLES"
  if [[ "$cycle" -gt 1 ]]; then
    echo "Resetting the lab cluster"
    (
      cd "$KUBESPRAY_DIR"
      ansible-playbook -i "$INVENTORY" reset.yml -b --become-user=root \
        -e reset_confirmation=yes
    ) 2>&1 | tee "$LOG_DIR/cycle${cycle}_reset.log"
  fi

  echo "Deploying the lab cluster"
  (
    cd "$KUBESPRAY_DIR"
    ansible-playbook -i "$INVENTORY" cluster.yml -b --become-user=root
  ) 2>&1 | tee "$LOG_DIR/cycle${cycle}_deploy.log"

  verify_cluster
  echo "Cycle $cycle passed"
done

echo "All $TOTAL_CYCLES destructive lab cycles passed"
