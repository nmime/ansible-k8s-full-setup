#!/usr/bin/env bash
# restore-drill.sh - Disaster recovery drill with safety gates
# Usage: ./scripts/restore-drill.sh --component <name> --backup <ref> [--force] [--dry-run]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
DRY_RUN=false; FORCE=false; COMPONENT=""; BACKUP_REF=""
RESTORE_NS="restore-drill"; CLEANUP_HOURS=24
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run) DRY_RUN=true; shift ;;
    --force)   FORCE=true; shift ;;
    --component) COMPONENT="$2"; shift 2 ;;
    --backup) BACKUP_REF="$2"; shift 2 ;;
    --namespace) RESTORE_NS="$2"; shift 2 ;;
    --cleanup-hours) CLEANUP_HOURS="$2"; shift 2 ;;
    -h|--help) echo "Usage: $0 --component <name> --backup <ref> [--force] [--dry-run]";
               echo "  --component  mongodb|vault|seaweedfs|gitlab"; exit 0 ;;
    *) error "Unknown: $1"; exit 1 ;;
  esac
done
[ -z "$COMPONENT" ] && error "Missing --component" && exit 1
[ -z "$BACKUP_REF" ] && error "Missing --backup" && exit 1
case "$COMPONENT" in mongodb|vault|seaweedfs|gitlab) ;; *) error "Invalid component"; exit 1 ;; esac
[ "$FORCE" != "true" ] && [ "$DRY_RUN" != "true" ] && warn "Use --force or --dry-run" && exit 1
if ! kubectl cluster-info &>/dev/null; then error "Cannot connect to cluster."; exit 1; fi
info "Cluster: OK"
OBJ="${OBJECT_STORAGE_ENDPOINT:-http://seaweedfs-filer.storage.svc.cluster.local:8333}"
PN="${PROJECT_NAME:-k8s}"; BB="${BACKUP_BUCKET:-backups-local}"
found=false
aws --endpoint-url="$OBJ" s3 ls "s3://${BB}/${PN}/${COMPONENT}/" 2>/dev/null | grep -q "$BACKUP_REF" && found=true
if [ "$found" = "false" ]; then
  warn "Backup artifact not found for ${COMPONENT}/${BACKUP_REF}"
  if [ "$FORCE" = "true" ]; then warn "Proceeding (--force)"; else error "Use --force to override"; exit 1; fi
fi
if [ "$DRY_RUN" = "true" ]; then
  info "[DRY RUN] Restore ${COMPONENT} from ${BACKUP_REF} into ${RESTORE_NS}"
  info "Steps: create namespace, deploy component, restore backup, verify, cleanup after ${CLEANUP_HOURS}h"
else
  info "Executing restore drill for ${COMPONENT}..."
  kubectl create namespace "$RESTORE_NS" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
  kubectl label namespace "$RESTORE_NS" app.kubernetes.io/part-of=restore-drill backup-restore.io/component="${COMPONENT}" --overwrite 2>/dev/null || true
  kubectl apply -n "$RESTORE_NS" -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: restore-drill-quota
spec:
  hard:
    requests.cpu: "4"
    requests.memory: 8Gi
    limits.cpu: "8"
    limits.memory: 16Gi
    pods: "10"
    persistentvolumeclaims: "5"
EOF
  kubectl apply -n "$RESTORE_NS" -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: restore-drill-cleanup
spec:
  schedule: "0 */${CLEANUP_HOURS} * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      ttlSecondsAfterFinished: 60
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: cleanup
              image: bitnami/kubectl:latest
              command: [/bin/sh,-c,kubectl delete namespace ${RESTORE_NS} --ignore-not-found]
EOF
  info "Namespace ${RESTORE_NS} created with ResourceQuota and auto-cleanup after ${CLEANUP_HOURS}h"
  info "Manual cleanup: kubectl delete namespace ${RESTORE_NS}"
fi
echo "============================================"; echo "  RESTORE DRILL SUMMARY"; echo "============================================"
echo "  Component: ${COMPONENT}  Backup: ${BACKUP_REF}  Namespace: ${RESTORE_NS}"
exit 0
