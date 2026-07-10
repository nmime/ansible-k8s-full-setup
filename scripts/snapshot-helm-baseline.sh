#!/bin/bash
# ============================================
# Snapshot / Helm Baseline Capture
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
SNAPSHOT_DIR="${PROJECT_ROOT}/snapshot"
SNAPSHOT_DRY_RUN=false

log()    { echo -e "\033[0;32m[$(date +'%Y-%m-%d %H:%M:%S')]\033[0m $1"; }
warn()   { echo -e "\033[1;33m[WARN]\033[0m $1"; }
error()  { echo -e "\033[0;31m[ERROR]\033[0m $1"; }
dry()    { echo -e "\033[1;33m[DRY-RUN]\033[0m $1"; }

parse_snapshot_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) SNAPSHOT_DRY_RUN=true; shift ;;
      *) shift ;;
    esac
  done
}

capture_snapshot() {
  local timestamp; timestamp=$(date +'%Y%m%d-%H%M%S')
  local snap_dir="${SNAPSHOT_DIR}/upgrade-${timestamp}"
  log "Capturing baseline snapshot to $snap_dir"
  if $SNAPSHOT_DRY_RUN; then
    dry "Would create snapshot at $snap_dir"
    dry "Would capture: helm releases, values, CRDs, namespaces, RBAC, PVs/PVCs, nodes"
    echo "$snap_dir"
    return 0
  fi
  mkdir -p "$snap_dir"/{helm,helm-values,rbac}
  log "  Helm releases..."
  helm list --all-namespaces --output yaml > "$snap_dir/helm/all-releases.yaml" 2>/dev/null || true
  for ns in $(kubectl get namespaces -o name 2>/dev/null | sed 's/namespace\///'); do
    helm list --namespace "$ns" --output yaml > "$snap_dir/helm/${ns}.yaml" 2>/dev/null || true
  done
  log "  Helm values..."
  for release in $(helm list --all-namespaces --output json 2>/dev/null | jq -r '.[].name' 2>/dev/null || true); do
    local ns
    ns=$(helm list --all-namespaces --output json 2>/dev/null | jq -r ".[] | select(.name==\"$release\") | .namespace" 2>/dev/null || echo "")
    [[ -n "$ns" && -n "$release" ]] && helm get values "$release" --namespace "$ns" --all > "$snap_dir/helm-values/${ns}-${release}.yaml" 2>/dev/null || true
  done
  log "  CRDs..."
  kubectl get crds -o yaml > "$snap_dir/crds.yaml" 2>/dev/null || true
  log "  Namespaces..."
  kubectl get namespaces -o yaml > "$snap_dir/namespaces.yaml" 2>/dev/null || true
  log "  RBAC..."
  kubectl get clusterroles -o yaml > "$snap_dir/rbac/clusterroles.yaml" 2>/dev/null || true
  kubectl get clusterrolebindings -o yaml > "$snap_dir/rbac/clusterrolebindings.yaml" 2>/dev/null || true
  log "  PVs/PVCs..."
  kubectl get pv -o yaml > "$snap_dir/pvs.yaml" 2>/dev/null || true
  kubectl get pvc --all-namespaces -o yaml > "$snap_dir/pvcs.yaml" 2>/dev/null || true
  log "  Nodes + version..."
  kubectl get nodes -o yaml > "$snap_dir/nodes.yaml" 2>/dev/null || true
  kubectl version > "$snap_dir/version.txt" 2>/dev/null || true
  log "  Manifest..."
  cat > "$snap_dir/MANIFEST.yaml" <<MANIFEST
snapshot_time: "$timestamp"
helm_releases:
$(helm list --all-namespaces --output json 2>/dev/null || echo '{}')
MANIFEST
  ln -sfn "$snap_dir" "${SNAPSHOT_DIR}/latest"
  log "Snapshot saved to $snap_dir"
  echo "$snap_dir"
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  parse_snapshot_args "$@"
  mkdir -p "$SNAPSHOT_DIR"
  capture_snapshot
fi
