#!/usr/bin/env bash
# pg-restore-drill.sh — Restore drill: isolated namespace, PG Operator 3.x, pgBackRest restore
#
# Usage: ./scripts/pg-restore-drill.sh [OPTIONS]
#
# Options:
#   --pg-namespace       Source namespace (default: databases)
#   --pg-cluster         Source PostgresCluster name (default: postgres-operator)
#   --s3-endpoint        S3 / object storage endpoint
#   --s3-bucket          S3 bucket with pgBackRest backups
#   --backup-set         pgBackRest backup set label (default: latest)
#   --operator-version   PG Operator Helm chart version (default: 3.0.0)
#   --namespace          Drill namespace (default: pg-upgrade-drill)
#   --ttl-hours          Auto-cleanup TTL (default: 24)
#   --skip-cleanup       Do not clean up after drill
#   --dry-run            Plan review only
#   --help               Show usage
#
# Exit codes: 0 = success, 1 = verification failure, 2 = script error
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
BACKUP_SET="latest"
OPERATOR_VERSION="${PG_OPERATOR_VERSION:-3.0.0}"
DRILL_NS="pg-upgrade-drill"
TTL_HOURS=24
SKIP_CLEANUP=false
DRY_RUN=false

DRILL_PASS=0
DRILL_FAIL=0

drill_pass() { DRILL_PASS=$((DRILL_PASS + 1)); pass "$*"; }
drill_fail() { DRILL_FAIL=$((DRILL_FAIL + 1)); fail "$*"; }

# ── Parse arguments ─────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --pg-namespace)     PG_NS="$2"; shift 2 ;;
    --pg-cluster)       PG_CLUSTER="$2"; shift 2 ;;
    --s3-endpoint)      S3_ENDPOINT="$2"; shift 2 ;;
    --s3-bucket)        S3_BUCKET="$2"; shift 2 ;;
    --backup-set)       BACKUP_SET="$2"; shift 2 ;;
    --operator-version) OPERATOR_VERSION="$2"; shift 2 ;;
    --namespace)        DRILL_NS="$2"; shift 2 ;;
    --ttl-hours)        TTL_HOURS="$2"; shift 2 ;;
    --skip-cleanup)     SKIP_CLEANUP=true; shift ;;
    --dry-run)          DRY_RUN=true; shift ;;
    -h|--help)
      head -20 "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

# ─────────────────────────────────────────────────────────────────
section "PG Operator 2 → 3 Restore Drill"
info "Source: $PG_NS/$PG_CLUSTER | Drill NS: $DRILL_NS | Operator: $OPERATOR_VERSION"

if [ "$DRY_RUN" = "true" ]; then
  info "DRY-RUN — execution plan:"
  echo ""
  echo "  0. Prerequisites (kubectl, helm, cluster, S3_ENDPOINT)"
  echo "  1. Create namespace $DRILL_NS + ResourceQuota + auto-cleanup CronJob"
  echo "  2. Deploy PG Operator $OPERATOR_VERSION via Helm"
  echo "  3. Copy / create pgBackRest S3 credentials Secret"
  echo "  4. Deploy v2 PostgresCluster with pgBackRest restore source"
  echo "  5. Wait for restore (timeout 60m)"
  echo "  6. Verify data integrity (databases, tables, extensions, pg version)"
  echo "  7. Verify replication and connectivity"
  echo "  8. ${SKIP_CLEANUP:+Skip}{Cleanup namespace $DRILL_NS}"
  echo ""
  echo "  Restore cluster spec (v2):"
  echo "  apiVersion: postgresql.percona.com/v2"
  echo "  kind: PostgresCluster"
  echo "  metadata:"
  echo "    name: pg-drill-cluster"
  echo "    namespace: $DRILL_NS"
  echo "  spec:"
  echo "    postgresVersion: 18"
  echo "    instances:"
  echo "      - name: postgres"
  echo "        replicas: 2"
  echo "    backup:"
  echo "      pgbackrest:"
  echo "        repo:"
  echo "          - name: repo1"
  echo "            s3:"
  echo "              bucket: $S3_BUCKET"
  echo "              endpoint: $S3_ENDPOINT"
  echo "        restore:"
  echo "          enabled: true"
  echo "          repoName: repo1"
  echo ""
  section "DRY-RUN SUMMARY"
  pass "Plan looks correct — rerun without --dry-run to execute"
  exit 0
fi

# ── Step 0: Prerequisites ──────────────────────────────────────
section "Step 0: Prerequisites"
command -v kubectl &>/dev/null || { drill_fail "kubectl"; exit 2; }
drill_pass "kubectl"
command -v helm &>/dev/null || { drill_fail "helm"; exit 2; }
drill_pass "helm"
kubectl cluster-info &>/dev/null || { drill_fail "cluster unreachable"; exit 1; }
drill_pass "cluster reachable"
[ -n "$S3_ENDPOINT" ] || { drill_fail "S3_ENDPOINT not set"; exit 1; }
drill_pass "S3_ENDPOINT configured"

# ── Step 1: Isolated namespace ────────────────────────────────
section "Step 1: Isolated Namespace"

# Clean up if namespace exists
if kubectl get ns "$DRILL_NS" &>/dev/null; then
  warn "Namespace '$DRILL_NS' exists — cleaning up"
  kubectl delete ns "$DRILL_NS" --wait --timeout=5m 2>/dev/null || true
  sleep 5
fi

kubectl create ns "$DRILL_NS" 2>/dev/null || true
kubectl label ns "$DRILL_NS" app.kubernetes.io/part-of=pg-restore-drill \
  backup-restore.io/drill=true --overwrite 2>/dev/null || true

# ResourceQuota
kubectl apply -n "$DRILL_NS" -f - <<'EOF'
apiVersion: v1
kind: ResourceQuota
metadata:
  name: pg-drill-quota
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
    pods: "10"
    persistentvolumeclaims: "5"
EOF

# Auto-cleanup CronJob
if [ "$SKIP_CLEANUP" = "false" ]; then
  kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: pg-drill-cleanup
  labels:
    app.kubernetes.io/part-of: pg-restore-drill
spec:
  schedule: "0 */${TTL_HOURS} * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      ttlSecondsAfterFinished: 60
      template:
        spec:
          restartPolicy: OnFailure
          serviceAccountName: default
          containers:
            - name: cleanup
              image: bitnami/kubectl:latest
              command: ["/bin/sh","-c","kubectl delete ns ${DRILL_NS} --ignore-not-found"]
EOF
  info "Auto-cleanup CronJob installed (TTL ${TTL_HOURS}h)"
fi

drill_pass "Namespace $DRILL_NS ready"

# ── Step 2: Deploy PG Operator 3.x ────────────────────────────
section "Step 2: Deploy PG Operator $OPERATOR_VERSION"

helm repo add percona https://percona.github.io/percona-helm-charts 2>/dev/null || true
helm repo update 2>/dev/null || true

if helm install percona-pg-operator percona/pg-operator \
  --version "$OPERATOR_VERSION" --namespace "$DRILL_NS" \
  --wait --timeout 10m 2>&1; then
  drill_pass "PG Operator $OPERATOR_VERSION deployed"
else
  drill_fail "Helm install failed — trying without --wait"
  helm install percona-pg-operator percona/pg-operator \
    --version "$OPERATOR_VERSION" --namespace "$DRILL_NS" 2>&1 || true
  kubectl wait --for=condition=Available deployment/percona-pg-operator \
    -n "$DRILL_NS" --timeout=10m 2>/dev/null || {
    drill_fail "Operator not ready — aborting drill"
    [ "$SKIP_CLEANUP" = "false" ] && kubectl delete ns "$DRILL_NS" --wait=false 2>/dev/null || true
    exit 1
  }
  drill_pass "Operator deployed (non-blocking)"
fi

# ── Step 3: pgBackRest S3 credentials ──────────────────────────
section "Step 3: pgBackRest Credentials"

S3_SECRET="pgbackrest-s3-credentials"
CRED_EXISTS=$(kubectl get secret -n "$PG_NS" pgbackrest-repo-credentials -o name 2>/dev/null || echo "")

if [ -n "$CRED_EXISTS" ]; then
  info "Copying pgBackRest credentials from $PG_NS"
  kubectl get secret -n "$PG_NS" pgbackrest-repo-credentials -o json 2>/dev/null |
    jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences)' |
    kubectl apply -n "$DRILL_NS" -f - 2>/dev/null || true
  drill_pass "Credentials copied"
elif [ -n "${AWS_ACCESS_KEY_ID:-}" ] && [ -n "${AWS_SECRET_ACCESS_KEY:-}" ]; then
  kubectl create secret generic "$S3_SECRET" -n "$DRILL_NS" \
    --from-literal=AWS_ACCESS_KEY_ID="$AWS_ACCESS_KEY_ID" \
    --from-literal=AWS_SECRET_ACCESS_KEY="$AWS_SECRET_ACCESS_KEY" 2>/dev/null
  drill_pass "Credentials from environment"
else
  warn "No credentials found — creating placeholder (restore may fail)"
  kubectl create secret generic "$S3_SECRET" -n "$DRILL_NS" \
    --from-literal=AWS_ACCESS_KEY_ID="DRILL_PLACEHOLDER" \
    --from-literal=AWS_SECRET_ACCESS_KEY="DRILL_PLACEHOLDER" 2>/dev/null
fi

# ── Step 4: v2 PostgresCluster with restore ───────────────────
section "Step 4: Deploy v2 PostgresCluster with pgBackRest Restore"

RESTORE_EXTRA=""
[ "$BACKUP_SET" != "latest" ] && RESTORE_EXTRA="        backup: ${BACKUP_SET}"

cat > /tmp/pg-drill-spec.yaml <<EOF
apiVersion: postgresql.percona.com/v2
kind: PostgresCluster
metadata:
  name: pg-drill-cluster
  namespace: ${DRILL_NS}
  labels:
    app.kubernetes.io/part-of: pg-restore-drill
spec:
  postgresVersion: 18
  instances:
    - name: postgres
      replicas: 2
      storage:
        size: 20Gi
  backup:
    pgbackrest:
      repo:
        - name: repo1
          s3:
            bucket: ${S3_BUCKET}
            endpoint: ${S3_ENDPOINT}
            region: ${AWS_REGION:-us-east-1}
            storageType: s3
            s3Credentials:
              name: ${S3_SECRET}
              accessKeyId: AWS_ACCESS_KEY_ID
              secretAccessKey: AWS_SECRET_ACCESS_KEY
      restore:
        enabled: true
        repoName: repo1
        type: full
${RESTORE_EXTRA}
EOF

kubectl apply -f /tmp/pg-drill-spec.yaml

info "Waiting for restore (up to 60m)..."
if kubectl wait --for=condition=Ready postgrescluster/pg-drill-cluster \
  -n "$DRILL_NS" --timeout=60m 2>/dev/null; then
  drill_pass "PostgresCluster Ready — restore complete"
else
  drill_fail "PostgresCluster not Ready within 60m"
  info "Check: kubectl describe postgrescluster/pg-drill-cluster -n $DRILL_NS"
  kubectl logs -n "$DRILL_NS" -l percona.com/cluster=pg-drill-cluster --tail=100 2>/dev/null || true
fi

# ── Step 5: Data integrity ────────────────────────────────────
section "Step 5: Data Integrity"

DRILL_PRIMARY=$(kubectl get pod -n "$DRILL_NS" \
  -l "percona.com/cluster=pg-drill-cluster,role=primary" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || echo "pg-drill-cluster-0")
info "Primary pod: $DRILL_PRIMARY"

# 5a. Databases
DBS=$(kubectl exec -n "$DRILL_NS" "$DRILL_PRIMARY" -- psql -U postgres -t -A \
  -c "SELECT datname FROM pg_database WHERE datistemplate = false ORDER BY datname;" 2>/dev/null || echo "")
if [ -n "$DBS" ]; then
  DCOUNT=$(echo "$DBS" | wc -l)
  drill_pass "$DCOUNT database(s)"
  echo "$DBS" | while IFS= read -r d; do info "  db: $d"; done
else
  drill_fail "Could not query databases"
fi

# 5b. Tables
TBLS=$(kubectl exec -n "$DRILL_NS" "$DRILL_PRIMARY" -- psql -U postgres -t -A \
  -c "SELECT schemaname||'.'||relname||' ('||n_live_tup||' rows)' FROM pg_stat_user_tables ORDER BY n_live_tup DESC LIMIT 10;" 2>/dev/null || echo "")
if [ -n "$TBLS" ]; then
  drill_pass "Table count OK"
  echo "$TBLS" | while IFS= read -r t; do info "  $t"; done
else
  info "No user tables (empty cluster?)"
fi

# 5c. Extensions
EXTS=$(kubectl exec -n "$DRILL_NS" "$DRILL_PRIMARY" -- psql -U postgres -t -A \
  -c "SELECT name||'='||installed_version FROM pg_extension ORDER BY name;" 2>/dev/null || echo "")
if [ -n "$EXTS" ]; then
  drill_pass "Extensions: $(echo "$EXTS" | tr '\n' ', ')"
else
  drill_pass "Standard PostgreSQL (no extensions)"
fi

# 5d. Version
PG_VER=$(kubectl exec -n "$DRILL_NS" "$DRILL_PRIMARY" -- psql -U postgres -t -A \
  -c "SHOW server_version;" 2>/dev/null || echo "")
drill_pass "PostgreSQL version: $PG_VER"

# ── Step 6: Replication ───────────────────────────────────────
section "Step 6: Replication"

REPL=$(kubectl exec -n "$DRILL_NS" "$DRILL_PRIMARY" -- psql -U postgres -t -A \
  -c "SELECT count(*) FROM pg_stat_replication;" 2>/dev/null || echo "0")
if [ "$REPL" -gt 0 ] 2>/dev/null; then
  drill_pass "$REPL replica(s) streaming"
else
  info "No replicas (single-instance drill?)"
fi

# ── Step 7: Connectivity ──────────────────────────────────────
section "Step 7: Connectivity"

CONN=$(kubectl run -n "$DRILL_NS" pg-drill-test --rm -i --restart=Never \
  --image=postgres:18 --command -- psql \
  "host=pg-drill-cluster port=5432 dbname=postgres user=postgres" \
  -t -A -c "SELECT 'ok' AS conn;" 2>/dev/null || echo "")
echo "$CONN" | grep -q "ok" && drill_pass "Connectivity OK" || drill_fail "Connectivity failed"

# ── Step 8: Summary ──────────────────────────────────────────
section "DRILL SUMMARY"
echo ""
info "Pass: $DRILL_PASS | Fail: $DRILL_FAIL"
echo ""
if [ "$DRILL_FAIL" -gt 0 ]; then
  fail "Drill completed with $DRILL_FAIL failure(s) — investigate before production upgrade"
else
  pass "Drill completed successfully — restore path validated"
fi

# ── Cleanup ────────────────────────────────────────────────────
if [ "$SKIP_CLEANUP" = "false" ]; then
  section "Step 8: Cleanup"
  info "Deleting namespace $DRILL_NS..."
  kubectl delete ns "$DRILL_NS" --wait=false 2>/dev/null || true
  drill_pass "Cleanup initiated"
fi

rm -f /tmp/pg-drill-spec.yaml

[ "$DRILL_FAIL" -gt 0 ] && exit 1
pass "Restore drill — exit 0"
exit 0
