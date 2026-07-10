#!/usr/bin/env bash
# backup-all.sh - Orchestrate full backup of all components
# Usage: ./scripts/backup-all.sh [--dry-run] [--component <name>] [--force]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
DRY_RUN=false; COMPONENT=""; FORCE=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=true; shift ;;
    --component) COMPONENT="$2"; shift 2 ;;
    --force)     FORCE=true; shift ;;
    -h|--help) echo "Usage: $0 [--dry-run] [--component <name>] [--force]";
               echo "  --component  mongodb|vault|seaweedfs|gitlab|all"; exit 0 ;;
    *) error "Unknown option: $1"; exit 1 ;;
  esac
done
# Safety Gate 1: Confirmation
if [ "${FORCE}" != "true" ]; then
  echo "============================================"; echo "  BACKUP ALL COMPONENTS"; echo "============================================"
  [ "${DRY_RUN}" = "true" ] && warn "DRY RUN MODE"
  echo ""; read -r -p "Proceed with backup? (yes/no): " CONFIRM
  [ "${CONFIRM}" != "yes" ] && info "Aborted." && exit 0
fi
# Safety Gate 2: kubectl connectivity
if ! kubectl cluster-info &>/dev/null; then error "Cannot connect to cluster."; exit 1; fi
info "Cluster: OK"
# Safety Gate 3: Object storage
OBJ="${OBJECT_STORAGE_ENDPOINT:-}"
[ -z "$OBJ" ] && OBJ=$(kubectl get secret -n storage seaweedfs-s3-config -o jsonpath='{.data.endpoint}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
[ -n "$OBJ" ] && curl -sf --max-time 5 "$OBJ" &>/dev/null && info "Storage reachable" || warn "Storage check skipped"
TS=$(date -u +%Y%m%dT%H%M%SZ); RF="${PROJECT_ROOT}/.backup-results-${TS}.log"
TOTAL=0; PASSED=0; FAILED=0; SKIPPED=0
check_comp() { kubectl get pods -n "$1" -l "$2" &>/dev/null 2>&1; }
run_backup() {
  local comp="$1" tag="$2" ns="$3" lbl="$4"; TOTAL=$((TOTAL+1))
  if ! check_comp "$ns" "$lbl"; then warn "'${comp}' not deployed"; SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"; return 0; fi
  if [ "$DRY_RUN" = "true" ]; then info "[DRY RUN] ${tag}"; PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"; return 0; fi
  if ansible-playbook -i "${PROJECT_ROOT}/inventory" -t "$tag" "${PROJECT_ROOT}/playbooks/deploy_platform.yml" 2>&1 | tee -a "$RF"; then
    PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"
  else FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"; fi
}
echo "Timestamp: ${TS}" > "$RF"
declare -A M
M[mongodb]="backup-mongodb|databases|app.kubernetes.io/name=percona-server-mongodb"
M[vault]="backup-vault|vault|app.kubernetes.io/name=vault"
M[seaweedfs]="backup-seaweedfs|storage|app.kubernetes.io/name=seaweedfs"
M[gitlab]="backup-gitlab|gitlab|app.kubernetes.io/name=gitlab"
if [ "$COMPONENT" = "all" ] || [ -z "$COMPONENT" ]; then
  for c in mongodb vault seaweedfs gitlab; do IFS='|' read -r t n l <<< "${M[$c]}"; run_backup "$c" "$t" "$n" "$l"; done
else
  IFS='|' read -r t n l <<< "${M[$COMPONENT]}"; run_backup "$COMPONENT" "$t" "$n" "$l"
fi
echo "============================================"; echo "  BACKUP SUMMARY"; echo "============================================"
echo "  Total: ${TOTAL}  Passed: ${PASSED}  Failed: ${FAILED}  Skipped: ${SKIPPED}"
[ "$FAILED" -gt 0 ] && error "Failed." && exit 1
info "Completed successfully."; exit 0
