#!/usr/bin/env bash
# gitlab-upgrade-check.sh — Preflight checks before GitLab major upgrade (18→19)
# Usage: ./scripts/gitlab-upgrade-check.sh [OPTIONS]
#
# Options:
#   --gitlab-namespace    Namespace where GitLab is deployed (default: gitlab)
#   --s3-endpoint         S3-compatible endpoint URL
#   --s3-bucket           S3 bucket for GitLab backups (default: backups.<project>)
#   --backup-max-age      Maximum backup age in hours (default: 24)
#   --min-disk-free       Minimum free disk percentage (default: 20)
#   --dry-run             Run checks without cluster access (for CI/testing)
#   --help                Show this help
#
# Exit codes:
#   0 — All checks passed
#   1 — One or more checks failed
#   2 — Script error (bad arguments, etc.)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"

# ── Color helpers ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Defaults ───────────────────────────────────────────────────
GITLAB_NS="gitlab"
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
S3_BUCKET="${BACKUP_BUCKET:-}"
BACKUP_MAX_AGE_HOURS=24
MIN_DISK_FREE=20
DRY_RUN=false

# ── Counters ──────────────────────────────────────────────────
PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check_pass() { PASS_COUNT=$((PASS_COUNT + 1)); pass "$*"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); fail "$*"; }
check_warn() { WARN_COUNT=$((WARN_COUNT + 1)); warn "$*"; }

# ── Parse arguments ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --gitlab-namespace)  GITLAB_NS="$2"; shift 2 ;;
    --s3-endpoint)       S3_ENDPOINT="$2"; shift 2 ;;
    --s3-bucket)         S3_BUCKET="$2"; shift 2 ;;
    --backup-max-age)    BACKUP_MAX_AGE_HOURS="$2"; shift 2 ;;
    --min-disk-free)     MIN_DISK_FREE="$2"; shift 2 ;;
    --dry-run)           DRY_RUN=true; shift ;;
    -h|--help)
      head -20 "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

# ── Helpers ───────────────────────────────────────────────────
exec_in_rails() {
  kubectl exec -n "$GITLAB_NS" deploy/gitlab-gitlab-rails -- "$@" 2>/dev/null
}

check_chart_version() {
  helm list -n "$GITLAB_NS" -o json 2>/dev/null | jq -r '.[] | select(.name=="gitlab") | .chart' 2>/dev/null || echo "unknown"
}

check_app_version() {
  exec_in_rails bundle exec rails runner "puts Gitlab::CurrentSettings.version_string" 2>/dev/null || echo "unknown"
}

# ────────────────────────────────────────────────────────────────
section "GitLab Upgrade Preflight Checks (18→19)"
if [ "$DRY_RUN" = "true" ]; then
  info "Running in DRY-RUN mode (no cluster access)"
fi

# ── CHECK 1: Tooling ──────────────────────────────────────────
section "1. Tooling"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Tooling checks skipped"
else
  if command -v kubectl &>/dev/null; then
    VER=$(kubectl version --client --short 2>/dev/null || kubectl version --client -o json 2>/dev/null | jq -r .clientVersion.gitVersion || echo "version unknown")
    check_pass "kubectl is available ($VER)"
  else
    check_fail "kubectl is not installed or not in PATH"
  fi

  if command -v helm &>/dev/null; then
    check_pass "helm is available ($(helm version --short 2>/dev/null || echo 'version unknown'))"
  else
    check_fail "helm is not installed or not in PATH"
  fi

  if command -v aws &>/dev/null; then
    check_pass "aws CLI is available"
  else
    check_warn "aws CLI not found; S3 backup checks will be skipped"
  fi
fi

# ── CHECK 2: Cluster Connectivity ─────────────────────────────
section "2. Cluster Connectivity"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Cluster connectivity check skipped"
else
  if kubectl cluster-info &>/dev/null; then
    check_pass "Cluster is reachable"
  else
    check_fail "Cannot reach Kubernetes cluster"
  fi

  if kubectl get namespace "$GITLAB_NS" &>/dev/null; then
    check_pass "GitLab namespace '$GITLAB_NS' exists"
  else
    check_fail "GitLab namespace '$GITLAB_NS' not found"
  fi
fi

# ── CHECK 3: Current GitLab Version ──────────────────────────
section "3. Current GitLab Version"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] GitLab version check skipped"
else
  CHART_VER=$(check_chart_version)
  APP_VER=$(check_app_version)
  info "Chart version: $CHART_VER"
  info "App version: $APP_VER"

  if [[ "$CHART_VER" != "unknown" ]]; then
    CHART_MAJOR=$(echo "$CHART_VER" | cut -d. -f1)
    if [[ "$CHART_MAJOR" == "9" ]]; then
      check_pass "Chart version $CHART_VER is on upgrade path (9.x → 10.x)"
    elif [[ "$CHART_MAJOR" == "10" ]]; then
      check_pass "Chart already on 10.x ($CHART_VER)"
    else
      check_warn "Chart version $CHART_VER is outside expected upgrade path"
    fi
  else
    check_warn "Could not determine Helm chart version"
  fi

  if [[ "$APP_VER" != "unknown" && -n "$APP_VER" ]]; then
    APP_MAJOR=$(echo "$APP_VER" | cut -d. -f1)
    if [[ "$APP_MAJOR" == "18" ]]; then
      check_pass "GitLab app version $APP_VER is on upgrade path (18.x → 19.x)"
    elif [[ "$APP_MAJOR" == "19" ]]; then
      check_pass "GitLab already on 19.x ($APP_VER)"
    else
      check_warn "GitLab app version $APP_VER is outside expected upgrade path"
    fi
  else
    check_warn "Could not determine GitLab app version"
  fi
fi

# ── CHECK 4: GitLab Pods Health ───────────────────────────────
section "4. GitLab Pods Health"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Pod health check skipped"
else
  TOTAL_PODS=$(kubectl get pods -n "$GITLAB_NS" -o json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)
  RUNNING_PODS=$(kubectl get pods -n "$GITLAB_NS" --field-selector=status.phase=Running -o json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)

  if [[ "$TOTAL_PODS" -eq 0 ]]; then
    check_fail "No pods found in namespace $GITLAB_NS"
  elif [[ "$RUNNING_PODS" -eq "$TOTAL_PODS" ]]; then
    check_pass "All $TOTAL_PODS GitLab pods are Running"
  else
    check_fail "$RUNNING_PODS/$TOTAL_PODS pods Running ($(( TOTAL_PODS - RUNNING_PODS )) not Running)"
    kubectl get pods -n "$GITLAB_NS" --field-selector=status.phase!=Running 2>/dev/null | tail -5
  fi

  # Check for CrashLoopBackOff
  CRASH_LOOPS=$(kubectl get pods -n "$GITLAB_NS" 2>/dev/null | grep -c "CrashLoopBackOff" || true)
  if [[ "$CRASH_LOOPS" -gt 0 ]]; then
    check_fail "$CRASH_LOOPS pod(s) in CrashLoopBackOff"
  else
    check_pass "No pods in CrashLoopBackOff"
  fi

  # Check for pending pods
  PENDING=$(kubectl get pods -n "$GITLAB_NS" 2>/dev/null | grep -c "Pending" || true)
  if [[ "$PENDING" -gt 0 ]]; then
    check_warn "$PENDING pod(s) in Pending state"
  else
    check_pass "No pods in Pending state"
  fi
fi

# ── CHECK 5: Backup Age ───────────────────────────────────────
section "5. Backup Age"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Backup age check skipped"
elif [ -z "$S3_ENDPOINT" ]; then
  check_warn "S3_ENDPOINT not set; cannot check backups"
else
  BUCKET_PATH="${S3_BUCKET:-backups}"
  SNAPSHOT_LIST=$(aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${BUCKET_PATH}/gitlab/" 2>/dev/null || echo "")

  if [ -z "$SNAPSHOT_LIST" ]; then
    check_fail "No GitLab backups found in s3://${BUCKET_PATH}/gitlab/"
  else
    LATEST_LINE=$(echo "$SNAPSHOT_LIST" | sort | tail -1)
    LATEST_NAME=$(echo "$LATEST_LINE" | awk '{print $4}')
    LATEST_TS=$(echo "$LATEST_LINE" | awk '{print $1}')
    LATEST_SIZE=$(echo "$LATEST_LINE" | awk '{print $3}')

    info "Latest backup: $LATEST_NAME ($LATEST_TS, $LATEST_SIZE bytes)"
    check_pass "GitLab backup found: $LATEST_NAME"

    # Check backup size (should be non-trivial)
    if [[ "$LATEST_SIZE" -gt 10485760 ]]; then
      check_pass "Backup size is reasonable ($(( LATEST_SIZE / 1048576 ))MB)"
    else
      check_warn "Backup may be too small ($(( LATEST_SIZE / 1024 ))KB) — verify it's a full backup"
    fi

    # Try to extract date from filename
    FILE_DATE=$(echo "$LATEST_NAME" | grep -oP '\d{8}_\d{6}' || echo "")
    if [ -n "$FILE_DATE" ]; then
      FILE_EPOCH=$(date -d "${FILE_DATE/_/T}" +%s 2>/dev/null || echo 0)
      NOW_EPOCH=$(date +%s)
      if [[ "$FILE_EPOCH" -gt 0 ]]; then
        AGE_HOURS=$(( (NOW_EPOCH - FILE_EPOCH) / 3600 ))
        if [[ "$AGE_HOURS" -le "$BACKUP_MAX_AGE_HOURS" ]]; then
          check_pass "Backup age ${AGE_HOURS}h is within ${BACKUP_MAX_AGE_HOURS}h limit"
        else
          check_fail "Backup is ${AGE_HOURS}h old (limit: ${BACKUP_MAX_AGE_HOURS}h)"
        fi
      fi
    fi
  fi
fi

# ── CHECK 6: S3 Connectivity ──────────────────────────────────
section "6. S3 Connectivity"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] S3 connectivity check skipped"
elif [ -z "$S3_ENDPOINT" ]; then
  check_warn "S3_ENDPOINT not set; skipping S3 connectivity check"
else
  if aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET:-backups}/" &>/dev/null; then
    check_pass "S3 endpoint is reachable"
  else
    check_fail "Cannot reach S3 endpoint: $S3_ENDPOINT"
  fi
fi

# ── CHECK 7: Disk Space ───────────────────────────────────────
section "7. Disk Space"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Disk space check skipped"
else
  # Check node disk usage
  NODE_USAGE=$(kubectl top nodes 2>/dev/null | tail -n +2 | head -1 || echo "")
  if [ -n "$NODE_USAGE" ]; then
    info "Node metrics: $NODE_USAGE"
    check_pass "Node metrics available"
  else
    check_warn "kubectl top not available (metrics-server may not be running)"
  fi

  # Check local disk (bastion/controller)
  LOCAL_USAGE=$(df -h / 2>/dev/null | tail -1 | awk '{print $5}' | tr -d '%')
  if [ -n "$LOCAL_USAGE" ]; then
    FREE=$((100 - LOCAL_USAGE))
    if [[ "$FREE" -ge "$MIN_DISK_FREE" ]]; then
      check_pass "Local disk usage ${LOCAL_USAGE}% (free: ${FREE}% >= ${MIN_DISK_FREE}%)"
    else
      check_fail "Local disk usage ${LOCAL_USAGE}% (free: ${FREE}% < ${MIN_DISK_FREE}%)"
    fi
  fi
fi

# ── CHECK 8: Gitaly Status ────────────────────────────────────
section "8. Gitaly Status"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Gitaly status check skipped"
else
  GITALY_PODS=$(kubectl get pods -n "$GITLAB_NS" -l app=gitaly -o json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)

  if [[ "$GITALY_PODS" -eq 0 ]]; then
    check_warn "No Gitaly pods found"
  else
    check_pass "$GITALY_PODS Gitaly pod(s) found"

    # Check Gitaly is responding
    GITALY_CHECK=$(kubectl exec -n "$GITLAB_NS" deploy/gitlab-gitaly -- gitaly-prrc healthcheck 2>&1 | tail -1 || echo "")
    if echo "$GITALY_CHECK" | grep -qi "ok\|healthy\|0 issues"; then
      check_pass "Gitaly health check passed"
    elif echo "$GITALY_CHECK" | grep -qi "issue\|error"; then
      check_fail "Gitaly health check reported issues: $GITALY_CHECK"
    else
      check_warn "Gitaly health check returned unexpected output: $GITALY_CHECK"
    fi

    # Check Gitaly storage PVCs
    GITALY_PVCS=$(kubectl get pvc -n "$GITLAB_NS" -l app=gitaly -o json 2>/dev/null | jq '.items | length' 2>/dev/null || echo 0)
    if [[ "$GITALY_PVCS" -gt 0 ]]; then
      check_pass "$GITALY_PVCS Gitaly PVC(s) found"
    else
      check_warn "No Gitaly PVCs found — storage may be ephemeral"
    fi
  fi
fi

# ── CHECK 9: PostgreSQL Connectivity ──────────────────────────
section "9. PostgreSQL Connectivity"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] PostgreSQL connectivity check skipped"
else
  DB_CHECK=$(kubectl exec -n "$GITLAB_NS" deploy/gitlab-gitlab-rails -- bundle exec rails db:check 2>&1 || echo "")
  if echo "$DB_CHECK" | grep -qi "true\|connected\|migration"; then
    check_pass "PostgreSQL connection is healthy"
  elif echo "$DB_CHECK" | grep -qi "false\|error\|cannot\|connection refused"; then
    check_fail "PostgreSQL connection failed: $DB_CHECK"
  else
    check_warn "PostgreSQL connection check returned: $DB_CHECK"
  fi
fi

# ── CHECK 10: Redis Connectivity ──────────────────────────────
section "10. Redis Connectivity"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Redis connectivity check skipped"
else
  REDIS_PING=$(kubectl exec -n "$GITLAB_NS" deploy/gitlab-gitlab-rails -- bundle exec rails runner "puts Redis.current.ping" 2>&1 || echo "")
  if echo "$REDIS_PING" | grep -qi "PONG"; then
    check_pass "Redis is responding (PONG)"
  else
    check_fail "Redis is not responding: $REDIS_PING"
  fi
fi

# ── CHECK 11: Helm Release Status ─────────────────────────────
section "11. Helm Release Status"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Helm release check skipped"
else
  RELEASE_STATUS=$(helm status gitlab -n "$GITLAB_NS" -o json 2>/dev/null | jq -r '.info.status // "unknown"' 2>/dev/null || echo "unknown")
  info "GitLab Helm release status: $RELEASE_STATUS"

  case "$RELEASE_STATUS" in
    deployed)
      check_pass "Helm release is deployed"
      ;;
    pending-install|pending-upgrade|pending-rollback)
      check_warn "Helm release is pending: $RELEASE_STATUS"
      ;;
    failed|superseded)
      check_fail "Helm release is in bad state: $RELEASE_STATUS"
      ;;
    *)
      check_warn "Helm release status is $RELEASE_STATUS"
      ;;
  esac
fi

# ── CHECK 12: Backup CronJob ──────────────────────────────────
section "12. Backup CronJob"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] Backup CronJob check skipped"
else
  if kubectl get cronjob gitlab-toolbox-backup -n "$GITLAB_NS" &>/dev/null; then
    check_pass "GitLab backup CronJob exists"

    # Check last scheduled time
    LAST_SCHEDULED=$(kubectl get cronjob gitlab-toolbox-backup -n "$GITLAB_NS" -o json 2>/dev/null | jq -r '.status.lastScheduleTime // "never"' 2>/dev/null)
    if [ "$LAST_SCHEDULED" != "never" ]; then
      info "Last backup scheduled: $LAST_SCHEDULED"
      check_pass "Backup CronJob has been scheduled"
    else
      check_warn "Backup CronJob has never been scheduled"
    fi
  else
    check_fail "GitLab backup CronJob not found in $GITLAB_NS"
  fi
fi

# ── SUMMARY ────────────────────────────────────────────────────
section "SUMMARY"
echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT"
echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT"
echo -e "  ${YELLOW}Warnings:${NC} $WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "\n${RED}${BOLD}❌ PREFLIGHT CHECKS FAILED — Do not proceed with upgrade${NC}"
  exit 1
else
  if [ "$WARN_COUNT" -gt 0 ]; then
    echo -e "\n${YELLOW}${BOLD}⚠ Preflight checks passed with $WARN_COUNT warning(s). Review before proceeding.${NC}"
  else
    echo -e "\n${GREEN}${BOLD}✅ All preflight checks passed — ready for upgrade${NC}"
  fi
  exit 0
fi
