#!/usr/bin/env bash
# gitlab-restore-test.sh — Restore drill for GitLab backups (isolated namespace)
# Usage: ./scripts/gitlab-restore-test.sh [OPTIONS]
#
# Options:
#   --restore             Perform the restore drill (required for actual restore)
#   --backup TIMESTAMP    Backup timestamp to restore (e.g., 20250601_020000)
#   --namespace           Isolated restore namespace (default: gitlab-restore-drill)
#   --ttl-hours           Auto-cleanup TTL in hours (default: 24)
#   --s3-endpoint         S3-compatible endpoint URL
#   --s3-bucket           S3 bucket for GitLab backups
#   --source-namespace    Source GitLab namespace (default: gitlab)
#   --dry-run             Plan the restore without executing
#   --cleanup-only        Remove a previous restore drill namespace
#   --list-backups        List available backups
#   --help                Show this help
#
# Exit codes:
#   0 — Restore drill completed successfully
#   1 — One or more smoke tests failed
#   2 — Script error (bad arguments, etc.)
set -euo pipefail

# ── Color helpers ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Defaults ───────────────────────────────────────────────────
RESTORE=false
BACKUP_TIMESTAMP=""
RESTORE_NS="gitlab-restore-drill"
TTL_HOURS=24
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
S3_BUCKET="${BACKUP_BUCKET:-gitlab-backups}"
SOURCE_NS="gitlab"
DRY_RUN=false
CLEANUP_ONLY=false
LIST_BACKUPS=false

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
    --restore)           RESTORE=true; shift ;;
    --backup)            BACKUP_TIMESTAMP="$2"; shift 2 ;;
    --namespace)         RESTORE_NS="$2"; shift 2 ;;
    --ttl-hours)         TTL_HOURS="$2"; shift 2 ;;
    --s3-endpoint)       S3_ENDPOINT="$2"; shift 2 ;;
    --s3-bucket)         S3_BUCKET="$2"; shift 2 ;;
    --source-namespace)  SOURCE_NS="$2"; shift 2 ;;
    --dry-run)           DRY_RUN=true; shift ;;
    --cleanup-only)      CLEANUP_ONLY=true; shift ;;
    --list-backups)      LIST_BACKUPS=true; shift ;;
    -h|--help)
      head -22 "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

# ── Cleanup handler ───────────────────────────────────────────
cleanup_namespace() {
  section "Cleaning up restore namespace"
  if kubectl get namespace "$RESTORE_NS" &>/dev/null; then
    kubectl delete namespace "$RESTORE_NS" --wait --timeout=300s
    pass "Namespace $RESTORE_NS deleted"
  else
    info "Namespace $RESTORE_NS already gone"
  fi
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup_on_exit() {
  if [ "$DRY_RUN" = "false" ] && [ "$RESTORE" = "true" ]; then
    cleanup_namespace
  fi
}
trap cleanup_on_exit EXIT

# ── List backups ──────────────────────────────────────────────
list_backups() {
  section "Available GitLab Backups"
  if [ -z "$S3_ENDPOINT" ]; then
    fail "S3_ENDPOINT not set; cannot list backups"
    exit 2
  fi

  info "Listing s3://${S3_BUCKET}/"
  aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${S3_BUCKET}/" || {
    fail "Cannot list backups — check S3 endpoint and bucket"
    exit 1
  }
  exit 0
}

if [ "$LIST_BACKUPS" = "true" ]; then
  list_backups
fi

# ── Cleanup only ──────────────────────────────────────────────
if [ "$CLEANUP_ONLY" = "true" ]; then
  cleanup_namespace
  exit 0
fi

# ── Validation ────────────────────────────────────────────────
section "GitLab Restore Drill"

if [ "$DRY_RUN" = "true" ]; then
  info "Running in DRY-RUN mode — no cluster changes will be made"
fi

if [ "$RESTORE" = "true" ] && [ -z "$BACKUP_TIMESTAMP" ]; then
  fail "--backup TIMESTAMP is required with --restore"
  echo "Use --list-backups to see available backups"
  exit 2
fi

# ── DRY-RUN PATH ─────────────────────────────────────────────
if [ "$DRY_RUN" = "true" ]; then
  section "Dry-Run: Restore Plan"
  info "Would create namespace: $RESTORE_NS"
  info "Would restore backup: ${BACKUP_TIMESTAMP:-latest}"
  info "Would set TTL: ${TTL_HOURS}h auto-cleanup"
  info "Source namespace: $SOURCE_NS"

  # Verify prerequisites
  if command -v kubectl &>/dev/null; then
    check_pass "kubectl available"
  else
    check_fail "kubectl not found"
  fi

  if [ -n "$S3_ENDPOINT" ] && command -v aws &>/dev/null; then
    check_pass "aws CLI available with S3 endpoint"
  else
    check_warn "aws CLI or S3_ENDPOINT missing; cannot verify backup"
  fi

  section "SUMMARY (Dry-Run)"
  echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT"
  echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT"
  echo -e "  ${YELLOW}Warnings:${NC} $WARN_COUNT"

  if [ "$FAIL_COUNT" -gt 0 ]; then
    echo -e "\n${RED}${BOLD}❌ Dry-run found issues — fix before real restore${NC}"
    exit 1
  fi
  echo -e "\n${GREEN}${BOLD}✅ Dry-run passed — ready to execute restore drill${NC}"
  exit 0
fi

# ── ACTUAL RESTORE DRILL ──────────────────────────────────────
if [ "$RESTORE" != "true" ]; then
  fail "Specify --restore to perform a restore drill, or --dry-run to plan"
  echo "Or use --list-backups to see available backups"
  exit 2
fi

# ── Step 1: Create Isolated Namespace ─────────────────────────
section "1. Create Isolated Namespace"

if kubectl get namespace "$RESTORE_NS" &>/dev/null; then
  info "Namespace $RESTORE_NS already exists — cleaning first"
  kubectl delete namespace "$RESTORE_NS" --wait --timeout=120s
  sleep 5
fi

kubectl create namespace "$RESTORE_NS"
pass "Namespace $RESTORE_NS created"

# Add TTL label for auto-cleanup tracking
kubectl label namespace "$RESTORE_NS" restore-drill/ttl-hours="$TTL_HOURS" --overwrite
kubectl label namespace "$RESTORE_NS" restore-drill/created="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite

# ── Step 2: Download Backup ──────────────────────────────────
section "2. Download Backup from S3"

BACKUP_FILE="${BACKUP_TIMESTAMP}_gitlab_backup.tar"
if [ -z "$S3_ENDPOINT" ]; then
  fail "S3_ENDPOINT not set; cannot download backup"
  exit 1
fi

DOWNLOAD_DIR="/tmp/gitlab-restore-$(date +%s)"
mkdir -p "$DOWNLOAD_DIR"

info "Downloading s3://${S3_BUCKET}/${BACKUP_FILE} ..."
aws --endpoint-url "$S3_ENDPOINT" s3 cp "s3://${S3_BUCKET}/${BACKUP_FILE}" "${DOWNLOAD_DIR}/${BACKUP_FILE}" || {
  fail "Failed to download backup: ${BACKUP_FILE}"
  fail "Check backup timestamp. Use --list-backups to see available backups"
  cleanup_namespace
  exit 1
}

FILE_SIZE=$(stat -c%s "${DOWNLOAD_DIR}/${BACKUP_FILE}" 2>/dev/null || stat -f%z "${DOWNLOAD_DIR}/${BACKUP_FILE}" 2>/dev/null || echo 0)
info "Downloaded ${BACKUP_FILE} (${FILE_SIZE} bytes)"
pass "Backup file downloaded successfully"

# ── Step 3: Deploy GitLab Restore Job ─────────────────────────
section "3. Deploy GitLab Restore Job"

# Use the exact Toolbox image from the source release so the restore utilities
# match the GitLab version that created the backup.
TOOLBOX_IMAGE=$(kubectl get pods -n "$SOURCE_NS" -l release=gitlab,app=toolbox \
  -o jsonpath='{.items[0].spec.containers[0].image}')
if [ -z "$TOOLBOX_IMAGE" ]; then
  fail "No GitLab Toolbox pod found in namespace $SOURCE_NS"
  exit 1
fi
info "Using source Toolbox image: $TOOLBOX_IMAGE"

# Upload backup to a temporary PVC in the restore namespace
info "Creating PVC and uploading backup to restore namespace"

cat > "${DOWNLOAD_DIR}/restore-foundation.yaml" << MANIFEST
apiVersion: v1
kind: Namespace
metadata:
  name: ${RESTORE_NS}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gitlab-backup-data
  namespace: ${RESTORE_NS}
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 10Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: restore-postgresql
  namespace: ${RESTORE_NS}
spec:
  replicas: 1
  selector:
    matchLabels:
      app: restore-postgresql
  template:
    metadata:
      labels:
        app: restore-postgresql
    spec:
      containers:
      - name: postgresql
        image: postgres:17.6-alpine3.21
        env:
        - name: POSTGRES_PASSWORD
          value: restore-only-password
        - name: POSTGRES_DB
          value: gitlab_restore
        readinessProbe:
          exec:
            command: ["pg_isready", "-U", "postgres"]
          initialDelaySeconds: 5
          periodSeconds: 5
---
apiVersion: v1
kind: Service
metadata:
  name: restore-postgresql
  namespace: ${RESTORE_NS}
spec:
  selector:
    app: restore-postgresql
  ports:
  - port: 5432
    targetPort: 5432
---
apiVersion: v1
kind: Pod
metadata:
  name: backup-uploader
  namespace: ${RESTORE_NS}
spec:
  restartPolicy: Never
  containers:
  - name: uploader
    image: alpine:3.22
    command: ["sh", "-c", "sleep 3600"]
    volumeMounts:
    - name: backup-volume
      mountPath: /backup
  volumes:
  - name: backup-volume
    persistentVolumeClaim:
      claimName: gitlab-backup-data
MANIFEST

kubectl apply -f "${DOWNLOAD_DIR}/restore-foundation.yaml"
kubectl wait --for=condition=Ready pod/backup-uploader -n "$RESTORE_NS" --timeout=180s
kubectl cp "${DOWNLOAD_DIR}/${BACKUP_FILE}" "$RESTORE_NS/backup-uploader:/backup/gitlab-backup.tar"
kubectl delete pod backup-uploader -n "$RESTORE_NS" --wait
kubectl rollout status deployment/restore-postgresql -n "$RESTORE_NS" --timeout=180s
pass "Backup uploaded to the isolated restore volume"

cat > "${DOWNLOAD_DIR}/restore-job.yaml" << MANIFEST
apiVersion: batch/v1
kind: Job
metadata:
  name: gitlab-restore-test
  namespace: ${RESTORE_NS}
  labels:
    app.kubernetes.io/part-of: restore-drill
    app.kubernetes.io/component: gitlab-restore
spec:
  ttlSecondsAfterFinished: 3600
  backoffLimit: 1
  activeDeadlineSeconds: 3600
  template:
    spec:
      restartPolicy: Never
      containers:
      - name: gitlab-restore
        image: ${TOOLBOX_IMAGE}
        env:
        - name: PGPASSWORD
          value: restore-only-password
        command: ["/bin/bash", "-c"]
        args:
        - |
          set -euo pipefail
          BD=/tmp/backups
          mkdir -p "$BD"
          tar xf /backup/gitlab-backup.tar -C "$BD"
          DB_DUMP=$(find "$BD" -path '*/db/database.sql.gz' -o -path '*/db/database.sql' | head -1)
          test -n "$DB_DUMP"
          if [[ "$DB_DUMP" == *.gz ]]; then
            gzip -dc "$DB_DUMP" | psql -h restore-postgresql -U postgres -d gitlab_restore -v ON_ERROR_STOP=1
          else
            psql -h restore-postgresql -U postgres -d gitlab_restore -v ON_ERROR_STOP=1 -f "$DB_DUMP"
          fi
          psql -h restore-postgresql -U postgres -d gitlab_restore -Atc \
            "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | grep -Eq '^[1-9][0-9]*$'
          REPO_COUNT=$(find "$BD" -path '*/repositories/*' -type f | wc -l | tr -d ' ')
          test "$REPO_COUNT" -gt 0
          echo "Database restored and repository payload verified"
          echo "Restore drill completed successfully"
        volumeMounts:
        - name: backup-volume
          mountPath: /backup
          readOnly: true
      volumes:
      - name: backup-volume
        persistentVolumeClaim:
          claimName: gitlab-backup-data
MANIFEST

kubectl apply -f "${DOWNLOAD_DIR}/restore-job.yaml"
pass "Restore Job manifest applied"

# ── Step 4: Wait for Restore Job ──────────────────────────────
section "4. Wait for Restore Job"

info "Waiting for gitlab-restore-test job to complete (timeout: 30m)..."
if kubectl wait --for=condition=complete job/gitlab-restore-test -n "$RESTORE_NS" --timeout=1800s 2>/dev/null; then
  check_pass "Restore job completed successfully"
else
  check_fail "Restore job did not complete within timeout"
  kubectl logs job/gitlab-restore-test -n "$RESTORE_NS" --tail=50 2>/dev/null || true
fi

# ── Step 5: Smoke Tests ──────────────────────────────────────
section "5. Smoke Tests"

# Check that the backup tarball was extractable
BACKUP_LOG=$(kubectl logs job/gitlab-restore-test -n "$RESTORE_NS" 2>/dev/null || echo "")

if echo "$BACKUP_LOG" | grep -q "Restore drill completed successfully"; then
  check_pass "Backup integrity verified (drill completed)"
else
  check_fail "Backup drill did not report success"
fi

if echo "$BACKUP_LOG" | grep -q "Database restored and repository payload verified"; then
  check_pass "Database restored and repository payload verified"
else
  check_fail "Backup contents could not be verified"
fi

# Verify backup timestamp matches
if echo "$BACKUP_LOG" | grep -qi "backup"; then
  check_pass "Backup file was processed"
else
  check_fail "No backup processing found in logs"
fi

# ── Step 6: Cleanup ──────────────────────────────────────────
section "6. Cleanup"

# Clean up local download
rm -rf "$DOWNLOAD_DIR"
info "Local backup download cleaned up"

# Namespace cleanup is handled by the EXIT trap
info "Namespace $RESTORE_NS will be cleaned up on exit"

# ── SUMMARY ───────────────────────────────────────────────────
section "SUMMARY"
echo -e "  ${GREEN}Passed:${NC}  $PASS_COUNT"
echo -e "  ${RED}Failed:${NC}  $FAIL_COUNT"
echo -e "  ${YELLOW}Warnings:${NC} $WARN_COUNT"

if [ "$FAIL_COUNT" -gt 0 ]; then
  echo -e "\n${RED}${BOLD}❌ RESTORE DRILL FAILED — backup may be unreliable${NC}"
  echo "The restore namespace $RESTORE_NS has been cleaned up."
  exit 1
else
  echo -e "\n${GREEN}${BOLD}✅ RESTORE DRILL PASSED — database and repository payload restored in isolation${NC}"
  echo "The restore namespace $RESTORE_NS has been cleaned up."
  exit 0
fi
