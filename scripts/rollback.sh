#!/bin/bash
# ============================================
# Platform Rollback Script
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
STATE_DIR="${PROJECT_ROOT}/.upgrade-state"
SNAPSHOT_DIR="${PROJECT_ROOT}/snapshot"
LOG_DIR="${PROJECT_ROOT}/logs"
LOG_FILE="${LOG_DIR}/rollback.log"

DRY_RUN=false; TARGET_TIER=""; COMPONENTS=""; FORCE=false
CUSTOM_SNAPSHOT=""

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'

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
    snap_dir=$(find "$SNAPSHOT_DIR" -mindepth 1 -maxdepth 1 -type d \
      -name 'upgrade-*' -print 2>/dev/null | sort -r | head -1)
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
      rollback_helm_release argocd argocd "$snapshot_dir" ;;
    cilium)
      rollback_helm_release kube-system cilium "$snapshot_dir" ;;
    cert-manager)
      rollback_helm_release cert-manager cert-manager "$snapshot_dir" ;;
    postgresql|database|databases)
      rollback_snapshot_namespace databases "$snapshot_dir" ;;
    observability)
      rollback_snapshot_namespace monitoring "$snapshot_dir" ;;
    gitlab)
      rollback_helm_release gitlab gitlab "$snapshot_dir" ;;
    *)
      error "Unknown component: $component"; return 1 ;;
  esac
}

rollback_helm_release() {
  local namespace="$1" release="$2" snapshot_dir="$3" revision
  revision=$(awk -F '\t' -v ns="$namespace" -v rel="$release" '$1==ns && $2==rel {print $3}' "${snapshot_dir}/helm-revisions.tsv")
  [[ -n "$revision" ]] || { error "No baseline revision for ${namespace}/${release}"; return 1; }
  helm rollback "$release" "$revision" -n "$namespace" --wait --timeout 30m0s
}

rollback_snapshot_namespace() {
  local namespace="$1" snapshot_dir="$2" ns release revision found=false
  while IFS=$'\t' read -r ns release revision; do
    [[ "$ns" == "$namespace" ]] || continue
    found=true
    helm rollback "$release" "$revision" -n "$namespace" --wait --timeout 30m0s
  done < "${snapshot_dir}/helm-revisions.tsv"
  [[ "$found" == "true" ]] || { error "No baseline releases in namespace $namespace"; return 1; }
}

rollback_tier() {
  local snapshot_dir="$1"
  log "  Full tier rollback using snapshot: $snapshot_dir"
  $DRY_RUN && { dry "Would restore tier from $snapshot_dir"; return 0; }
  [[ -f "${snapshot_dir}/platform.yaml" ]] || { error "Snapshot has no platform.yaml"; return 1; }
  [[ -f "${snapshot_dir}/helm-revisions.tsv" ]] || { error "Snapshot has no Helm revision baseline"; return 1; }
  while IFS=$'\t' read -r namespace release revision; do
    [[ -n "$release" ]] || continue
    helm rollback "$release" "$revision" -n "$namespace" --wait --timeout 30m0s
  done < "${snapshot_dir}/helm-revisions.tsv"
  local active_config="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
  cp "$active_config" "${active_config}.pre-rollback-$(date +%Y%m%d%H%M%S)"
  cp "${snapshot_dir}/platform.yaml" "$active_config"
  if [[ -n "$TARGET_TIER" ]]; then
    local restored_tier
    restored_tier=$(yq -r '.tier' "$active_config")
    [[ "$restored_tier" == "$TARGET_TIER" ]] || {
      error "Snapshot tier $restored_tier does not match requested $TARGET_TIER"; return 1;
    }
  fi
}

main() {
  parse_args "$@"
  mkdir -p "$LOG_DIR" "$STATE_DIR"
  log "=== ROLLBACK STARTED ==="
  log "Components: ${COMPONENTS:-all} | Dry run: $DRY_RUN"

  [[ "$DRY_RUN" != "true" && "$FORCE" != "true" ]] && {
    read -rp "Rollback ${COMPONENTS:-all} components? [y/N] " c
    [[ "$c" != "y" && "$c" != "Y" ]] && { info "Cancelled"; exit 0; }
  }

  local snap_dir; snap_dir=$(find_latest_snapshot) || exit 1
  info "Using snapshot: $snap_dir"

  COMPONENTS="${COMPONENTS:-all}"
  if [[ "$COMPONENTS" == "all" ]]; then
    rollback_tier "$snap_dir"
  else
    IFS=',' read -ra comp_arr <<< "$COMPONENTS"
    for comp in "${comp_arr[@]}"; do rollback_component "$comp" "$snap_dir"; done
  fi

  log "Post-rollback health check..."
  # shellcheck source=./scripts/health-gates.sh
  source "${SCRIPT_DIR}/health-gates.sh"
  local health_args=()
  [[ "$DRY_RUN" == "true" ]] && health_args+=(--dry-run)
  if check_health_gates "${health_args[@]}"; then
    log "Rollback completed - health gates PASSED"
  else
    error "Rollback failed health gates"
    cat > "$STATE_DIR/rollback-failed.json" <<EOF
{"components":"${COMPONENTS}","snapshot":"$snap_dir","status":"failed","timestamp":"$(date -Iseconds)"}
EOF
    exit 1
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
