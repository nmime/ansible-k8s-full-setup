#!/bin/bash
# ============================================
# Platform Upgrade Orchestrator
# ============================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
STATE_DIR="${PROJECT_ROOT}/.upgrade-state"
SNAPSHOT_DIR="${PROJECT_ROOT}/snapshot"
LOG_DIR="${PROJECT_ROOT}/logs"
LOG_FILE="${LOG_DIR}/upgrade.log"

TIER_ORDER=("minimal" "small" "medium" "production")

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; NC='\033[0m'

DRY_RUN=false; COMPONENTS=""; FORCE=false
SKIP_PREFLIGHT=false; TARGET_TIER=""; COMMAND=""
LOGGING_ACTIVE=false

log()    { echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1" | tee -a "${LOG_FILE}"; }
warn()   { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "${LOG_FILE}"; }
error()  {
  if "$LOGGING_ACTIVE"; then
    echo -e "${RED}[ERROR]${NC} $1" | tee -a "${LOG_FILE}" >&2
  else
    echo -e "${RED}[ERROR]${NC} $1" >&2
  fi
}
info()   { echo -e "${CYAN}[INFO]${NC} $1" | tee -a "${LOG_FILE}"; }
dry()    { echo -e "${YELLOW}[DRY-RUN]${NC} $1" | tee -a "${LOG_FILE}"; }

usage() {
  cat <<EOF
Usage: $(basename "$0") [OPTIONS] <command>

Commands:
  plan       Generate upgrade plan
  execute    Run upgrade with canary phases
  preflight  Run preflight compatibility checks
  snapshot   Capture helm/cluster baseline snapshot
  validate   Validate current cluster health

Options:
  --dry-run          Simulate without changes
  --tier TIER        Target tier (default: from platform.yaml)
  --component NAME   Upgrade specific component (repeatable)
  --force            Skip confirmations
  --skip-preflight   Skip preflight checks
  --verbose          Verbose output
  -h, --help         Show this help

Examples:
  $(basename "$0") --dry-run plan
  $(basename "$0") --dry-run execute
  $(basename "$0") execute --component argocd
  $(basename "$0") snapshot
  $(basename "$0") preflight
EOF
  exit 0
}

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run)        DRY_RUN=true; shift ;;
      --tier)           TARGET_TIER="$2"; shift 2 ;;
      --component)      COMPONENTS="${COMPONENTS:+${COMPONENTS},}$2"; shift 2 ;;
      --force)          FORCE=true; shift ;;
      --skip-preflight) SKIP_PREFLIGHT=true; shift ;;
      --verbose)        set -x; shift ;;
      -h|--help)        usage ;;
      plan|execute|preflight|snapshot|validate)
        [[ -n "$COMMAND" ]] && { error "Multiple commands"; usage; }
        COMMAND="$1"; shift ;;
      *) error "Unknown option: $1"; exit 1 ;;
    esac
  done
  [[ -z "$COMMAND" ]] && { error "Command required"; exit 1; }
}

load_config() {
  [[ ! -f "$CONFIG_FILE" ]] && { error "Config not found: $CONFIG_FILE"; exit 1; }
  PROJECT=$(yq '.global.project // "k8s"' "$CONFIG_FILE")
  CURRENT_TIER=$(yq '.tier // "small"' "$CONFIG_FILE")
  DOMAIN=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
  EMAIL=$(yq -r '.global.email // ""' "$CONFIG_FILE")
  [[ -z "$TARGET_TIER" ]] && TARGET_TIER="$CURRENT_TIER"
  info "Config: project=$PROJECT tier=$CURRENT_TIER target=$TARGET_TIER domain=$DOMAIN"
}

get_canary_sequence() {
  local target="$1" current_idx=-1 target_idx=-1
  for i in "${!TIER_ORDER[@]}"; do
    [[ "${TIER_ORDER[$i]}" == "$CURRENT_TIER" ]] && current_idx=$i
    [[ "${TIER_ORDER[$i]}" == "$target" ]] && target_idx=$i
  done
  [[ "$current_idx" -ge 0 ]] || { error "Unknown current tier: $CURRENT_TIER"; return 1; }
  [[ "$target_idx" -ge 0 ]] || { error "Unknown target tier: $target"; return 1; }
  [[ "$target_idx" -ge "$current_idx" ]] || { error "Tier downgrade requires rollback.sh"; return 1; }
  local seq=()
  if [[ "$target_idx" -eq "$current_idx" ]]; then
    seq+=("$target")
  else
    for i in $(seq $((current_idx + 1)) "$target_idx"); do seq+=("${TIER_ORDER[$i]}"); done
  fi
  echo "${seq[@]}"
}

upgrade_component() {
  local component="$1" tier="$2"
  log "  Upgrading component: $component (tier=$tier)"
  case "$component" in
    argocd)
      $DRY_RUN && { dry "helm upgrade argocd argo/argo-cd -n argocd"; return 0; }
      helm upgrade argocd argo/argo-cd -n argocd --version 9.5.14 --reuse-values --atomic --wait --timeout 10m0s ;;
    cilium)
      $DRY_RUN && { dry "helm upgrade cilium -n kube-system"; return 0; }
      helm upgrade cilium cilium/cilium -n kube-system --reuse-values --atomic --wait --timeout 10m0s ;;
    cert-manager)
      $DRY_RUN && { dry "helm upgrade cert-manager -n cert-manager"; return 0; }
      helm upgrade cert-manager jetstack/cert-manager -n cert-manager --reuse-values --atomic --wait --timeout 5m0s ;;
    postgresql|database|databases)
      $DRY_RUN && { dry "ansible-playbook deploy_platform.yml --tags databases"; return 0; }
      ansible-playbook "${PROJECT_ROOT}/playbooks/deploy_platform.yml" -e "@${CONFIG_FILE}" -e "tier=${tier}" --tags databases ;;
    observability)
      $DRY_RUN && { dry "ansible-playbook deploy_platform.yml --tags observability"; return 0; }
      ansible-playbook "${PROJECT_ROOT}/playbooks/deploy_platform.yml" -e "@${CONFIG_FILE}" -e "tier=${tier}" --tags observability ;;
    gitlab)
      upgrade_gitlab ;;
    *) error "Unknown component: $component"; return 1 ;;
  esac
}

upgrade_gitlab() {
  local current_chart targets=() target
  if $DRY_RUN; then
    dry "Would inspect the current GitLab chart and upgrade one GitLab minor at a time: 9.11.x -> 10.0.4 -> 10.1.2"
    return 0
  fi
  current_chart=$(helm list -n gitlab --output json | jq -r '.[0].chart // empty')
  case "$current_chart" in
    gitlab-9.11.*) targets=(10.0.4 10.1.2) ;;
    gitlab-10.0.*) targets=(10.1.2) ;;
    gitlab-10.1.2) log "GitLab is already at chart 10.1.2"; return 0 ;;
    *) error "Unsupported GitLab upgrade source chart: ${current_chart:-missing}"; return 1 ;;
  esac
  for target in "${targets[@]}"; do
    log "  GitLab chart upgrade to $target"
    helm upgrade gitlab gitlab/gitlab -n gitlab --version "$target" \
      --reuse-values --atomic --wait --timeout 30m0s
    kubectl wait --for=condition=complete -n gitlab \
      'job' --selector=release=gitlab --timeout=1800s
    bash "${SCRIPT_DIR}/gitlab-upgrade-check.sh" --gitlab-namespace gitlab
  done
}

run_canary_phase() {
  local tier="$1"
  log "=== CANARY PHASE: $tier ==="
  if $DRY_RUN; then
    dry "Would upgrade cluster to tier=$tier${COMPONENTS:+ components=$COMPONENTS}"
    return 0
  fi
  if [[ -n "$COMPONENTS" ]]; then
    IFS=',' read -ra comp_arr <<< "$COMPONENTS"
    for comp in "${comp_arr[@]}"; do upgrade_component "$comp" "$tier"; done
  else
    log "  Full platform deployment for tier=$tier"
    ansible-playbook "${PROJECT_ROOT}/playbooks/deploy_platform.yml" \
      -e "@${CONFIG_FILE}" \
      -e "tier=${tier}" -e "project_name=${PROJECT}" \
      -e "domain=${DOMAIN}" -e "email=${EMAIL}"
  fi
  local rc=$?
  [[ $rc -ne 0 ]] && { error "Canary phase $tier failed (rc=$rc)"; return 1; }
  log "  Post-canary health gates..."
  # shellcheck source=./scripts/health-gates.sh
  source "${SCRIPT_DIR}/health-gates.sh"
  if check_health_gates; then
    log "Canary phase $tier PASSED"
  else
    error "Canary phase $tier health gates FAILED"
    return 1
  fi
  mkdir -p "$STATE_DIR"
  echo "{\"tier\":\"$tier\",\"status\":\"passed\",\"timestamp\":\"$(date -Iseconds)\"}" > "$STATE_DIR/canary-${tier}.json"
}

generate_plan() {
  log "=== UPGRADE PLAN ==="
  info "Project:        $PROJECT"
  info "Current tier:   $CURRENT_TIER"
  info "Target tier:    $TARGET_TIER"
  info "Dry run:        $DRY_RUN"
  info "Components:     ${COMPONENTS:-ALL}"
  info "Skip preflight: $SKIP_PREFLIGHT"
  local seq; seq=$(get_canary_sequence "$TARGET_TIER")
  info "Canary sequence: $seq"
  ! $DRY_RUN && command -v helm &>/dev/null && {
    log "--- Current Helm releases ---"
    helm list --all-namespaces 2>/dev/null || warn "Could not list Helm releases"
  }
  local latest_snapshot
  latest_snapshot=$(find "$SNAPSHOT_DIR" -mindepth 1 -maxdepth 1 -type d \
    -name 'upgrade-*' -print 2>/dev/null | sort -r | head -1)
  if [[ -n "$latest_snapshot" ]]; then
    info "Latest snapshot: $latest_snapshot"
  else
    warn "No snapshots - consider running '$(basename "$0") snapshot'"
  fi
  log "=== END PLAN ==="
}

execute_upgrade() {
  log "=== STARTING UPGRADE ==="
  log "Target: $TARGET_TIER | Components: ${COMPONENTS:-ALL} | Dry: $DRY_RUN"

  [[ "$DRY_RUN" != "true" && "$FORCE" != "true" ]] && {
    read -rp "Upgrade to tier $TARGET_TIER? [y/N] " c
    [[ "$c" != "y" && "$c" != "Y" ]] && { info "Cancelled"; exit 0; }
  }

  if [[ "$SKIP_PREFLIGHT" != "true" ]]; then
    log "--- Step 1: Preflight ---"
    python3 "${SCRIPT_DIR}/preflight_check.py" --project-root "$PROJECT_ROOT" --dry-run="$DRY_RUN" || {
      error "Preflight failed. Use --skip-preflight to override."; exit 1
    }
  else
    warn "Preflight skipped"
  fi

  log "--- Step 2: Snapshot ---"
  if [[ "$DRY_RUN" == "true" ]]; then
    bash "${SCRIPT_DIR}/backup-all.sh" --dry-run --force
  else
    bash "${SCRIPT_DIR}/backup-all.sh" --force
  fi
  # shellcheck source=./scripts/snapshot-helm-baseline.sh
  source "${SCRIPT_DIR}/snapshot-helm-baseline.sh"
  export SNAPSHOT_DRY_RUN="$DRY_RUN"
  local snap; snap=$(capture_snapshot)

  log "--- Step 3: Canary phases ---"
  local seq; seq=$(get_canary_sequence "$TARGET_TIER")
  info "Sequence: $seq"
  for tier in $seq; do
    if ! run_canary_phase "$tier"; then
      error "Canary FAILED at tier=$tier - initiating rollback"
      $DRY_RUN || bash "${SCRIPT_DIR}/rollback.sh" --tier "$tier" --component "${COMPONENTS:-all}" || \
        warn "Rollback also encountered issues"
      exit 1
    fi
    [[ "$DRY_RUN" != "true" ]] && { log "  Stabilization pause 30s"; sleep 30; }
  done

  log "--- Step 4: Final health gate ---"
  # shellcheck source=./scripts/health-gates.sh
  source "${SCRIPT_DIR}/health-gates.sh"
  local health_args=()
  [[ "$DRY_RUN" == "true" ]] && health_args+=(--dry-run)
  if ! check_health_gates "${health_args[@]}"; then
    error "Final health gates failed - rollback"
    $DRY_RUN || bash "${SCRIPT_DIR}/rollback.sh" || warn "Rollback issues"
    exit 1
  fi

  mkdir -p "$STATE_DIR"
  cat > "$STATE_DIR/upgrade-complete.json" <<EOF2
{
  "target_tier": "$TARGET_TIER",
  "components": "${COMPONENTS:-all}",
  "canary_sequence": "$seq",
  "status": "completed",
  "timestamp": "$(date -Iseconds)",
  "snapshot": "${snap:-unknown}"
}
EOF2
  log "=== UPGRADE COMPLETED SUCCESSFULLY ==="
  log "  Tier: $TARGET_TIER | Components: ${COMPONENTS:-ALL} | Snapshot: $snap"
}

validate_health() {
  log "=== VALIDATE CLUSTER HEALTH ==="
  local rc=0
  python3 "${SCRIPT_DIR}/preflight_check.py" --project-root "$PROJECT_ROOT" --dry-run="$DRY_RUN" || rc=1
  # shellcheck source=./scripts/health-gates.sh
  source "${SCRIPT_DIR}/health-gates.sh"
  local health_args=()
  [[ "$DRY_RUN" == "true" ]] && health_args+=(--dry-run)
  check_health_gates "${health_args[@]}" || rc=1
  if [[ $rc -eq 0 ]]; then
    log "Validation PASSED"
  else
    error "Validation FAILED"
  fi
  return $rc
}

main() {
  parse_args "$@"
  mkdir -p "$STATE_DIR" "$SNAPSHOT_DIR" "$LOG_DIR"
  LOGGING_ACTIVE=true
  load_config
  case "$COMMAND" in
    plan)      generate_plan ;;
    execute)   execute_upgrade ;;
    preflight) python3 "${SCRIPT_DIR}/preflight_check.py" --project-root "$PROJECT_ROOT" --dry-run="$DRY_RUN" ;;
    snapshot)
      export SNAPSHOT_DRY_RUN="$DRY_RUN"
      # shellcheck source=./scripts/snapshot-helm-baseline.sh
      source "${SCRIPT_DIR}/snapshot-helm-baseline.sh"
      capture_snapshot
      ;;
    validate)  validate_health ;;
  esac
}

# Only run main when invoked directly, not when sourced
if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
