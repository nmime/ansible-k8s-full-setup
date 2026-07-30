#!/usr/bin/env bash
# gitlab-restore-test.sh — Integrity drill for GitLab Toolbox archives
# Usage: ./scripts/gitlab-restore-test.sh [OPTIONS]
#
# Options:
#   --restore             Perform the restore drill (required for actual restore)
#   --backup TIMESTAMP    Backup timestamp to restore (e.g., 20250601_020000)
#   --namespace           Isolated restore namespace (default: gitlab-restore-drill)
#   --ttl-hours           Auto-cleanup TTL in hours (default: 24)
#   --s3-endpoint         S3-compatible endpoint URL
#   --s3-bucket           S3 bucket for GitLab backups
#   --s3-credentials-secret Source Secret with accesskey and secretkey
#   --source-namespace    Source GitLab namespace (default: gitlab)
#   --storage-size        Isolated restore PVC size (default: 20Gi)
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
RESTORE=false
BACKUP_TIMESTAMP=""
RESTORE_NS="gitlab-restore-drill"
TTL_HOURS=24
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
S3_BUCKET="${BACKUP_BUCKET:-gitlab-backups}"
SOURCE_NS="gitlab"
S3_CREDENTIALS_SECRET="gitlab-object-storage"
STORAGE_SIZE="20Gi"
DRY_RUN=false
CLEANUP_ONLY=false
LIST_BACKUPS=false
RESTORE_NAMESPACE_CREATED=false

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
    --s3-credentials-secret) S3_CREDENTIALS_SECRET="$2"; shift 2 ;;
    --source-namespace)  SOURCE_NS="$2"; shift 2 ;;
    --storage-size)      STORAGE_SIZE="$2"; shift 2 ;;
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
  if kubectl get namespace "$RESTORE_NS" --request-timeout=10s &>/dev/null; then
    kubectl delete namespace "$RESTORE_NS" --wait --timeout=300s
    pass "Namespace $RESTORE_NS deleted"
  else
    info "Namespace $RESTORE_NS already gone"
  fi
}

# Invoked indirectly by the EXIT trap.
# shellcheck disable=SC2317,SC2329
cleanup_on_exit() {
  if [ "$RESTORE_NAMESPACE_CREATED" = "true" ]; then
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

  local list_pod
  list_pod="gitlab-backup-list-$(date +%s)"
  info "Listing s3://${S3_BUCKET}/ from an in-cluster, noninteractive pod"
  kubectl apply -n "$SOURCE_NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${list_pod}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 300
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: list
      image: amazon/aws-cli:2.34.48
      command: ["/bin/sh", "-c"]
      args: ["aws --endpoint-url=\"\$AWS_ENDPOINT_URL\" s3 ls s3://\$BACKUP_BUCKET/"]
      env:
        - name: HOME
          value: /tmp
        - name: AWS_ENDPOINT_URL
          value: "${S3_ENDPOINT}"
        - name: BACKUP_BUCKET
          value: "${S3_BUCKET}"
        - name: AWS_DEFAULT_REGION
          value: us-east-1
        - name: AWS_ACCESS_KEY_ID
          valueFrom:
            secretKeyRef:
              name: ${S3_CREDENTIALS_SECRET}
              key: accesskey
        - name: AWS_SECRET_ACCESS_KEY
          valueFrom:
            secretKeyRef:
              name: ${S3_CREDENTIALS_SECRET}
              key: secretkey
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
EOF
  if ! kubectl wait -n "$SOURCE_NS" "pod/$list_pod" \
    --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null; then
    kubectl logs -n "$SOURCE_NS" "$list_pod" --tail=100 >&2 || true
    kubectl delete pod -n "$SOURCE_NS" "$list_pod" --wait=true >/dev/null 2>&1 || true
    fail "Cannot list backups — check S3 endpoint and bucket"
    exit 1
  fi
  kubectl logs -n "$SOURCE_NS" "$list_pod"
  kubectl delete pod -n "$SOURCE_NS" "$list_pod" --wait=true >/dev/null
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

if [ -n "$BACKUP_TIMESTAMP" ] && [[ ! "$BACKUP_TIMESTAMP" =~ ^[A-Za-z0-9._-]+$ ]]; then
  fail "--backup must be an object name or backup ID containing only letters, digits, dot, underscore, and dash"
  exit 2
fi
if [[ ! "$STORAGE_SIZE" =~ ^[1-9][0-9]*(Mi|Gi|Ti)$ ]]; then
  fail "--storage-size must be a positive Kubernetes binary quantity such as 20Gi"
  exit 2
fi
if [[ ! "$RESTORE_NS" =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]]; then
  fail "--namespace must be a valid DNS label"
  exit 2
fi
if [[ ! "$S3_BUCKET" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]]; then
  fail "--s3-bucket contains unsupported characters"
  exit 2
fi
if [ -n "$S3_ENDPOINT" ] && [[ ! "$S3_ENDPOINT" =~ ^https?://[A-Za-z0-9.-]+(:[0-9]{1,5})?$ ]]; then
  fail "--s3-endpoint must be an HTTP(S) host with an optional numeric port"
  exit 2
fi
for tool in kubectl jq; do
  command -v "$tool" >/dev/null || { fail "$tool is required"; exit 2; }
done

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

  if [ -n "$S3_ENDPOINT" ]; then
    check_pass "S3 endpoint supplied for the in-cluster downloader"
  else
    check_warn "S3_ENDPOINT missing; cannot verify backup"
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
RESTORE_NAMESPACE_CREATED=true
pass "Namespace $RESTORE_NS created"

# Add TTL label for auto-cleanup tracking
kubectl label namespace "$RESTORE_NS" restore-drill/ttl-hours="$TTL_HOURS" --overwrite
kubectl annotate namespace "$RESTORE_NS" restore-drill/created="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite

# ── Step 2: Download Backup ──────────────────────────────────
section "2. Download Backup from S3"

case "$BACKUP_TIMESTAMP" in
  *_gitlab_backup.tar) BACKUP_FILE="$BACKUP_TIMESTAMP" ;;
  *) BACKUP_FILE="${BACKUP_TIMESTAMP}_gitlab_backup.tar" ;;
esac
if [ -z "$S3_ENDPOINT" ]; then
  fail "S3_ENDPOINT not set; cannot download backup"
  exit 1
fi

kubectl apply -n "$RESTORE_NS" -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: gitlab-restore-drill-quota
spec:
  hard:
    pods: "5"
    persistentvolumeclaims: "1"
    requests.storage: ${STORAGE_SIZE}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: gitlab-backup-data
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: ${STORAGE_SIZE}
EOF

kubectl get secret "$S3_CREDENTIALS_SECRET" -n "$SOURCE_NS" -o json \
  | jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields) | .metadata.name="gitlab-restore-s3"' \
  | kubectl apply -n "$RESTORE_NS" -f - >/dev/null

info "Downloading s3://${S3_BUCKET}/${BACKUP_FILE} directly into isolated persistent storage"
kubectl apply -n "$RESTORE_NS" -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: gitlab-backup-downloader
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 1800
  template:
    spec:
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: downloader
          image: amazon/aws-cli:2.34.48
          command: ["/bin/sh", "-c"]
          args:
            - >-
              set -eu;
              expected=\$(aws --endpoint-url="\$AWS_ENDPOINT_URL" s3api head-object
              --bucket "\$BACKUP_BUCKET" --key "\$BACKUP_KEY"
              --query ContentLength --output text);
              aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 cp
              "s3://\$BACKUP_BUCKET/\$BACKUP_KEY" /backup/gitlab-backup.tar;
              actual=\$(wc -c < /backup/gitlab-backup.tar | tr -d ' ');
              test "\$actual" = "\$expected";
              test "\$actual" -gt 0;
              printf 'downloaded-bytes=%s\n' "\$actual"
          env:
            - name: HOME
              value: /tmp
            - name: AWS_ENDPOINT_URL
              value: "${S3_ENDPOINT}"
            - name: BACKUP_BUCKET
              value: "${S3_BUCKET}"
            - name: BACKUP_KEY
              value: "${BACKUP_FILE}"
            - name: AWS_DEFAULT_REGION
              value: us-east-1
            - name: AWS_ACCESS_KEY_ID
              valueFrom:
                secretKeyRef:
                  name: gitlab-restore-s3
                  key: accesskey
            - name: AWS_SECRET_ACCESS_KEY
              valueFrom:
                secretKeyRef:
                  name: gitlab-restore-s3
                  key: secretkey
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: backup-volume
              mountPath: /backup
      volumes:
        - name: backup-volume
          persistentVolumeClaim:
            claimName: gitlab-backup-data
EOF
if ! kubectl wait --for=condition=complete job/gitlab-backup-downloader \
  -n "$RESTORE_NS" --timeout=30m; then
  kubectl logs job/gitlab-backup-downloader -n "$RESTORE_NS" --tail=100 >&2 || true
  fail "Failed to download or verify ${BACKUP_FILE}"
  exit 1
fi
kubectl logs job/gitlab-backup-downloader -n "$RESTORE_NS" --tail=1
kubectl delete job gitlab-backup-downloader -n "$RESTORE_NS" --wait=true >/dev/null
kubectl apply -n "$RESTORE_NS" -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: gitlab-restore-network-isolation
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
EOF
pass "Backup file size verified and network-isolated on the restore PVC"

# ── Step 3: Deploy isolated archive verification Job ──────────
section "3. Deploy GitLab Toolbox Archive Verification Job"

# Use the exact Toolbox image from the source release so the restore utilities
# match the GitLab version that created the backup.
TOOLBOX_IMAGE=$(kubectl get pods -n "$SOURCE_NS" -l release=gitlab,app=toolbox \
  -o jsonpath='{.items[0].spec.containers[0].image}')
if [ -z "$TOOLBOX_IMAGE" ]; then
  fail "No GitLab Toolbox pod found in namespace $SOURCE_NS"
  exit 1
fi
info "Using source Toolbox image: $TOOLBOX_IMAGE"

kubectl apply -n "$RESTORE_NS" -f - <<MANIFEST
apiVersion: batch/v1
kind: Job
metadata:
  name: gitlab-restore-test
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
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
      - name: gitlab-restore
        image: ${TOOLBOX_IMAGE}
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        command: ["/bin/bash", "-c"]
        args:
        - |
          set -euo pipefail
          BD=/backup/extracted
          rm -rf "\$BD"
          mkdir -p "\$BD"
          tar tf /backup/gitlab-backup.tar > "\$BD/contents.txt"
          if awk 'BEGIN { bad=0 } /^\// || /(^|\/)\.\.($|\/)/ { bad=1 } END { exit bad ? 0 : 1 }' "\$BD/contents.txt"; then
            echo "Unsafe absolute or parent-relative archive member" >&2
            exit 1
          fi
          tar --no-same-owner --no-same-permissions -xf /backup/gitlab-backup.tar -C "\$BD"
          find "\$BD" -name backup_information.yml -type f | grep -q .
          grep -Eq '(^|/)(repositories|repositories\.tar)' "\$BD/contents.txt"
          if grep -Eq '(^|/)db/database\.sql(\.gz)?$' "\$BD/contents.txt"; then
            echo "Unexpected database dump: external Percona PostgreSQL must use its native paired backup" >&2
            exit 1
          fi
          echo "Toolbox archive metadata and repository component verified"
          echo "Database is covered by the separately gated Percona pgBackRest backup"
          echo "Restore drill completed successfully"
        volumeMounts:
        - name: backup-volume
          mountPath: /backup
      volumes:
      - name: backup-volume
        persistentVolumeClaim:
          claimName: gitlab-backup-data
MANIFEST

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

if echo "$BACKUP_LOG" | grep -q "Toolbox archive metadata and repository component verified"; then
  check_pass "Toolbox archive metadata and repository component verified"
else
  check_fail "Backup contents could not be verified"
fi

if echo "$BACKUP_LOG" | grep -q "separately gated Percona pgBackRest backup"; then
  check_pass "External PostgreSQL recovery dependency recorded"
else
  check_fail "External PostgreSQL recovery dependency was not enforced"
fi

# Verify backup timestamp matches
if echo "$BACKUP_LOG" | grep -qi "backup"; then
  check_pass "Backup file was processed"
else
  check_fail "No backup processing found in logs"
fi

# ── Step 6: Cleanup ──────────────────────────────────────────
section "6. Cleanup"

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
  echo -e "\n${GREEN}${BOLD}✅ RESTORE DRILL PASSED — Toolbox archive verified in isolation${NC}"
  echo "The restore namespace $RESTORE_NS has been cleaned up."
  exit 0
fi
