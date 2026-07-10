#!/usr/bin/env bash
# pg-upgrade-check.sh — Preflight checks before Percona PG Operator 2→3 upgrade
#
# Usage: ./scripts/pg-upgrade-check.sh [OPTIONS]
#
# Options:
#   --pg-namespace       Namespace of the PG cluster (default: databases)
#   --pg-cluster         PostgresCluster CR name (default: postgres-operator)
#   --s3-endpoint        S3 / object storage endpoint URL
#   --s3-bucket          S3 bucket for pgBackRest backups
#   --backup-max-age     Max acceptable backup age in hours (default: 24)
#   --min-disk-gb        Min free disk space in GB (default: 20)
#   --dry-run            Run without cluster access (CI / offline)
#   --help               Show usage
#
# Exit codes: 0 = pass, 1 = failure, 2 = script error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# ── Colour helpers ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Defaults ────────────────────────────────────────────────────
PG_NS="databases"
PG_CLUSTER="postgres-operator"
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
S3_BUCKET="${PGBACKREST_BUCKET:-pgbackrest-backups}"
BACKUP_MAX_AGE=24
MIN_DISK_GB=20
DRY_RUN=false

PASS_COUNT=0
FAIL_COUNT=0
WARN_COUNT=0

check_pass() { PASS_COUNT=$((PASS_COUNT + 1)); pass "$*"; }
check_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); fail "$*"; }
check_warn() { WARN_COUNT=$((WARN_COUNT + 1)); warn "$*"; }

# ── Parse arguments ─────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pg-namespace)   PG_NS="$2"; shift 2 ;;
    --pg-cluster)     PG_CLUSTER="$2"; shift 2 ;;
    --s3-endpoint)    S3_ENDPOINT="$2"; shift 2 ;;
    --s3-bucket)      S3_BUCKET="$2"; shift 2 ;;
    --backup-max-age) BACKUP_MAX_AGE="$2"; shift 2 ;;
    --min-disk-gb)    MIN_DISK_GB="$2"; shift 2 ;;
    --dry-run)        DRY_RUN=true; shift ;;
    -h|--help)
      head -18 "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

# ── Helpers ─────────────────────────────────────────────────────
exec_in_pg() {
  kubectl exec -n "$PG_NS" "${PG_CLUSTER}-0" -- "$@" 2>/dev/null
}

# ─────────────────────────────────────────────────────────────────
section "PG Operator 2 → 3 Upgrade Preflight"
info "Namespace: $PG_NS | Cluster: $PG_CLUSTER"
if [ "$DRY_RUN" = "true" ]; then
  info "DRY-RUN mode — no cluster access"
fi

# ── 1. Tooling ─────────────────────────────────────────────────
section "1. Tooling"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] tooling skipped"
else
  command -v kubectl &>/dev/null && check_pass "kubectl" \
    || check_fail "kubectl not found"

  command -v helm &>/dev/null && check_pass "helm" \
    || check_fail "helm not found"

  if [ -n "$S3_ENDPOINT" ]; then
    command -v aws &>/dev/null && check_pass "aws CLI" \
      || check_fail "aws CLI not found"
  else
    check_warn "S3_ENDPOINT not set — S3 checks will be skipped"
  fi
fi

# ── 2. PG Operator version ──────────────────────────────────────
section "2. PG Operator Version"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] version check skipped"
else
  RELEASE=$(helm status percona-pg-operator -n "$PG_NS" -o json 2>/dev/null || echo "{}")
  STATUS=$(echo "$RELEASE" | jq -r '.info.status // "unknown"' 2>/dev/null)
  VERSION=$(echo "$RELEASE" | jq -r '.chart.metadata.version // "unknown"' 2>/dev/null)

  [ "$STATUS" = "deployed" ] && check_pass "Helm release deployed" \
    || check_warn "Helm release status: $STATUS"
  info "Chart version: $VERSION"

  if [ "$VERSION" = "3.0.0" ]; then
    check_pass "Operator at 3.0.0 — upgrade complete"
  elif [ "$VERSION" = "2.8.2" ]; then
    check_warn "Operator at 2.8.2 — run PG Operator 2→3 upgrade"
  else
    check_warn "Operator version $VERSION (expected 3.0.0)"
  fi
fi

# ── 3. PostgresCluster CR ───────────────────────────────────────
section "3. PostgresCluster CR"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] CR check skipped"
else
  if kubectl get postgrescluster "$PG_CLUSTER" -n "$PG_NS" &>/dev/null; then
    check_pass "PostgresCluster '$PG_CLUSTER' exists"
  else
    check_fail "PostgresCluster '$PG_CLUSTER' not found"
  fi
fi

# ── 4. Primary pod health ──────────────────────────────────────
section "4. Primary Pod Health"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] pod health skipped"
else
  PRIMARY=$(kubectl get pod -n "$PG_NS" -l "percona.com/cluster=${PG_CLUSTER},role=primary" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$PRIMARY" ]; then
    PHASE=$(kubectl get pod "$PRIMARY" -n "$PG_NS" -o jsonpath='{.status.phase}')
    [ "$PHASE" = "Running" ] && check_pass "Primary $PRIMARY is Running" \
      || check_fail "Primary $PRIMARY is $PHASE"
  else
    check_fail "No primary pod found"
  fi
fi

# ── 5. Replica lag ──────────────────────────────────────────────
section "5. Replica Lag"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] replica lag skipped"
else
  LAG=$(exec_in_pg psql -U postgres -t -A -c "
    SELECT MAX(pg_wal_lsn_diff(sent_lsn, replay_lsn))
    FROM pg_stat_replication;" 2>/dev/null || echo "")
  if [ -n "$LAG" ] && [ "$LAG" != "" ]; then
    if [ "$LAG" -le 1048576 ] 2>/dev/null; then
      check_pass "Max replica lag: ${LAG} bytes (within 1 MB)"
    else
      check_fail "Max replica lag: ${LAG} bytes (exceeds 1 MB)"
    fi
  else
    check_warn "Could not determine replica lag — check manually"
  fi
fi

# ── 6. pgBackRest backup age ────────────────────────────────────
section "6. pgBackRest Backup"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] backup check skipped"
else
  BACKUP_INFO=$(kubectl exec -n "$PG_NS" -c pgbackrest -- pgbackrest info 2>/dev/null || echo "")
  if [ -n "$BACKUP_INFO" ]; then
    check_pass "pgBackRest info retrievable"
    # Try to find the latest full backup timestamp
    LATEST_TS=$(echo "$BACKUP_INFO" | grep -oP '\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}' | tail -1 || echo "")
    if [ -n "$LATEST_TS" ]; then
      LATEST_EPOCH=$(date -d "$LATEST_TS" +%s 2>/dev/null || echo "0")
      AGE_H=$(( ($(date +%s) - LATEST_EPOCH) / 3600 ))
      if [ "$AGE_H" -le "$BACKUP_MAX_AGE" ]; then
        check_pass "Latest full backup ${AGE_H}h old (max ${BACKUP_MAX_AGE}h)"
      else
        check_fail "Latest full backup ${AGE_H}h old (max ${BACKUP_MAX_AGE}h) — re-backup"
      fi
    else
      check_warn "Could not parse backup timestamp — verify manually"
    fi
  else
    check_fail "pgBackRest info not available — run a backup"
  fi
fi

# ── 7. S3 / object storage ──────────────────────────────────────
section "7. S3 Repository"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] S3 check skipped"
elif [ -z "$S3_ENDPOINT" ]; then
  check_warn "S3_ENDPOINT not set"
else
  S3_OUT=$(aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET}/backup/" 2>&1 || echo "")
  if [ -n "$S3_OUT" ]; then
    FILE_COUNT=$(echo "$S3_OUT" | wc -l)
    check_pass "Found $FILE_COUNT files in s3://${S3_BUCKET}/backup/"
  else
    check_fail "No backup files in s3://${S3_BUCKET}/backup/"
  fi
fi

# ── 8. Disk space ──────────────────────────────────────────────
section "8. Disk Space"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] disk space skipped"
else
  PVC_TOTAL=$(kubectl get pvc -n "$PG_NS" -l "percona.com/cluster=${PG_CLUSTER}" \
    -o jsonpath='{range .items[*]}{.spec.resources.requests.storage}{"\n"}{end}' 2>/dev/null \
    | grep -oP '[0-9]+' | awk '{s+=$1} END {print s+0}')
  if [ "$PVC_TOTAL" -gt 0 ]; then
    GB=$((PVC_TOTAL / 1024))
    NEED=$((GB * 2))
    info "Current PVC total: ~${GB} Gi; restore needs ~${NEED} Gi"
    [ "$NEED" -le "$MIN_DISK_GB" ] && check_pass "Within disk budget" \
      || check_warn "Restore may need ${NEED} Gi (budget ${MIN_DISK_GB} Gi)"
  else
    check_warn "Could not determine PVC sizes"
  fi
fi

# ── 9. PgBouncer ────────────────────────────────────────────────
section "9. PgBouncer"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] PgBouncer check skipped"
else
  PGB=$(kubectl get svc -n "$PG_NS" -l "percona.com/component=proxy" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "")
  if [ -n "$PGB" ]; then
    PORT=$(kubectl get svc "$PGB" -n "$PG_NS" -o jsonpath='{.spec.ports[0].port}' 2>/dev/null)
    check_pass "PgBouncer service: $PGB:$PORT"
  else
    check_warn "No PgBouncer service found"
  fi
fi

# ── 10. Helm chart availability ────────────────────────────────
section "10. PG Operator 3.0 Chart"
if [ "$DRY_RUN" = "true" ]; then
  check_pass "[DRY-RUN] chart check skipped"
else
  helm repo add percona https://percona.github.io/percona-helm-charts 2>/dev/null || true
  helm repo update 2>/dev/null || true
  CHART=$(helm search repo percona/pg-operator --versions 2>/dev/null | grep "3.0" || echo "")
  [ -n "$CHART" ] && check_pass "PG Operator 3.0 chart available" \
    || check_warn "PG Operator 3.0 chart not found in repo"
fi

# ─────────────────────────────────────────────────────────────────
section "PREFLIGHT SUMMARY"
info "Pass: $PASS_COUNT | Fail: $FAIL_COUNT | Warn: $WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo ""
  fail "$FAIL_COUNT check(s) FAILED — do not proceed with upgrade."
  echo "Fix failures, then re-run: ./scripts/pg-upgrade-check.sh"
  exit 1
fi

[ "$WARN_COUNT" -gt 0 ] && echo "" && warn "$WARN_COUNT warning(s) — review before proceeding."
echo ""
pass "All preflight checks passed."
exit 0
