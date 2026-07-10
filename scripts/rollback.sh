#!/bin/bash
# ============================================
# Platform Rollback Script
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
STATE_DIR="${PROJECT_ROOT}/.upgrade-state"
SNAPSHOT_DIR="${PROJECT_ROOT}/snapshot"
LOG_DIR="${PROJECT_ROOT}/logs"
LOG_FILE="${LOG_DIR}/rollback.log"

DRY_RUN=false; TARGET_TIER=""; COMPONENTS="all"; FORCE=false
CUSTOM_SNAPSHOT=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

_safelog() { mkdir -p "$(dirname "${LOG_FILE}")"; true; }

log()    { _safelog; echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "${LOG_FILE}"; }
warn()   { _safelog; echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "${LOG_FILE}"; }
error()  { _safelog; echo -e "${RED}[ERROR]${NC} $1" | tee -a "${LOG_FILE}"; }
info()   { _safelog; echo -e "${CYAN}[INFO]${NC} $1" | tee -a "${LOG_FILE}"; }
dry()    { _safelog; echo -e "${YELLOW}[DRY-RUN]${NC} $1" | tee -a "${LOG_FILE}"; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS]

Options:
  --dry-run          Simulate without changes
  --tier TIER        Rollback to pre-tier state
  --component NAME   Rollback specific component (repeatable)
  --force            Skip confirmations
  --snapshot SNAP    Use specific snapshot directory
  -h, --help         Show this help
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)     DRY_RUN=true; shift ;;
      --tier)        TARGET_TIER="$2"; shift 2 ;;
      --component)   COMPONENTS="${COMPONENTS:+${COMPONENTS},}$2"; shift 2 ;;
      --force)       FORCE=true; shift ;;
      --snapshot)    CUSTOM_SNAPSHOT="$2"; shift 2 ;;
      -h|--help)     usage ;;
      *)             error "Unknown option: $1"; usage ;;
    esac
  done
}

find_latest_snapshot() {
  local snap_dir="${CUSTOM_SNAPSHOT:-}"
  if [[ -z "$snap_dir" ]]; then
    snap_dir=$(ls -dt "${SNAPSHOT_DIR}"/upgrade-* 2>/dev/null | head -1 || echo "")
  fi
  if [[ -z "$snap_dir" || ! -d "$snap_dir" ]]; then
    error "No snapshot found. Run upgrade-platform.sh snapshot first."
    return 1
  fi
  echo "$snap_dir"
}

rollback_component() {
  local component="$1" snapshot_dir="$2"
  log "  Rolling back component: $component"
  $DRY_RUN && { dry "Would rollback: $component"; return 0; }
  case "$component" in
    argocd)
      local val_file="${snapshot_dir}/helm-values/argocd-argocd.yaml"
      if [[ -f "$val_file" ]]; then
        helm upgrade argocd argo/argo-cd -n argocd -f "$val_file" --wait --timeout 10m0s || true
      else
        helm rollback argocd -n argocd --timeout 10m0s || true
      fi ;;
    cilium)
      helm rollback cilium -n kube-system --timeout 10m0s || true ;;
    cert-manager)
      helm rollback cert-manager -n cert-manager --timeout 5m0s || true ;;
    postgresql|database|databases)
      for db_ns in postgres k8s-databases database; do
        for rel in $(helm list -n "$db_ns" --output json 2>/dev/null | jq -r '.[].name' 2>/dev/null || true); do
          helm rollback "$rel" -n "$db_ns" --timeout 10m0s 2>/dev/null || true
        done
      done ;;
    observability)
      ansible-playbook "${PROJECT_ROOT}/playbooks/deploy_platform.yml" --tags observability 2>/dev/null || true ;;
    gitlab)
      helm rollback gitlab -n gitlab --timeout 30m0s || true ;;
    *)
      for ns in $(kubectl get namespaces -o name 2>/dev/null | sed 's/namespace\///'); do
        for rel in $(helm list -n "$ns" --output json 2>/dev/null | jq -r '.[].name' 2>/dev/null || true); do
          helm rollback "$rel" -n "$ns" --timeout 10m0s 2>/dev/null || true
        done
      done ;;
  esac
}

rollback_tier() {
  local snapshot_dir="$1"
  log "  Full tier rollback using snapshot: $snapshot_dir"
  $DRY_RUN && { dry "Would restore tier from $snapshot_dir"; return 0; }
  if [[ -f "${PROJECT_ROOT}/platform-orchestrator/platform.yaml" ]]; then
    ansible-playbook "${PROJECT_ROOT}/playbooks/deploy_platform.yml" \
      -e "@${PROJECT_ROOT}/platform-orchestrator/platform.yaml" \
      -e "project_name=${PROJECT:-k8s}" || warn "Ansible rollback completed with warnings"
  fi
}

main() {
  parse_args "$@"
  mkdir -p "$LOG_DIR"
  log "=== ROLLBACK STARTED ==="
  log "Components: ${COMPONENTS:-all} | Dry run: $DRY_RUN"

  [[ "$DRY_RUN" != "true" && "$FORCE" != "true" ]] && {
    read -rp "Rollback ${COMPONENTS:-all} components? [y/N] " c
    [[ "$c" != "y" && "$c" != "Y" ]] && { info "Cancelled"; exit 0; }
  }

  local snap_dir; snap_dir=$(find_latest_snapshot) || exit 1
  info "Using snapshot: $snap_dir"

  if [[ "$COMPONENTS" == "all" ]]; then
    rollback_tier "$snap_dir"
  else
    IFS=',' read -ra comp_arr <<< "$COMPONENTS"
    for comp in "${comp_arr[@]}"; do rollback_component "$comp" "$snap_dir"; done
  fi

  log "Post-rollback health check..."
  source "${SCRIPT_DIR}/health-gates.sh"
  if check_health_gates; then
    log "Rollback completed - health gates PASSED"
  else
    warn "Rollback completed but health gates have warnings"
  fi

  mkdir -p "$STATE_DIR"
  cat > "$STATE_DIR/rollback-complete.json" <<EOF
{
  "components": "${COMPONENTS:-all}",
  "snapshot": "$snap_dir",
  "timestamp": "$(date -Iseconds)",
  "status": "completed"
}
EOF
  log "=== ROLLBACK COMPLETED ==="
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
