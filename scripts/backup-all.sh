#!/usr/bin/env bash
# backup-all.sh - Orchestrate full backup of all components
# Usage: ./scripts/backup-all.sh [--dry-run] [--component <name>] [--config FILE] [--force]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }
DRY_RUN=false; COMPONENT=""; FORCE=false; CONFIG_FILE=""
[[ ! -f "${PROJECT_ROOT}/platform-orchestrator/platform.yaml" ]] \
  || CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=true; shift ;;
    --component) COMPONENT="$2"; shift 2 ;;
    --config)    CONFIG_FILE="$2"; shift 2 ;;
    --force)     FORCE=true; shift ;;
    -h|--help) echo "Usage: $0 [--dry-run] [--component <name>] [--config FILE] [--force]";
               echo "  --component  postgresql|mongodb|vault|seaweedfs|gitlab|all"; exit 0 ;;
    *) error "Unknown option: $1"; exit 1 ;;
  esac
done
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || { error "Platform config not found: $CONFIG_FILE"; exit 1; }
  command -v yq >/dev/null 2>&1 || { error "yq is required with --config"; exit 1; }
fi

component_expected() {
  local component="$1" expression
  [ -n "$CONFIG_FILE" ] || return 1
  case "$component" in
    postgresql) expression='.databases.enabled and .databases.postgresql.enabled' ;;
    mongodb) expression='.databases.enabled and .databases.mongodb.enabled' ;;
    vault) expression='.secrets.enabled' ;;
    seaweedfs) expression='.storage.enabled' ;;
    gitlab|gitlab-secrets) expression='.gitlab.enabled' ;;
    *) return 1 ;;
  esac
  [ "$(yq -r "${expression} // false" "$CONFIG_FILE")" = true ]
}

dry_run_component_is_disabled() {
  local component="$1"
  [ "$DRY_RUN" = true ] && [ -n "$CONFIG_FILE" ] \
    && { [ -z "$COMPONENT" ] || [ "$COMPONENT" = all ]; } \
    && ! component_expected "$component"
}
# Safety Gate 1: Confirmation
if [ "${FORCE}" != "true" ]; then
  echo "============================================"; echo "  BACKUP ALL COMPONENTS"; echo "============================================"
  [ "${DRY_RUN}" = "true" ] && warn "DRY RUN MODE"
  echo ""; read -r -p "Proceed with backup? (yes/no): " CONFIRM
  [ "${CONFIRM}" != "yes" ] && info "Aborted." && exit 0
fi
# Safety Gate 2: kubectl connectivity
if [ "$DRY_RUN" != "true" ]; then
  if ! kubectl cluster-info &>/dev/null; then error "Cannot connect to cluster."; exit 1; fi
  info "Cluster: OK"
else
  info "[DRY RUN] Would verify cluster connectivity"
fi
# Safety Gate 3: Object storage
OBJ="${OBJECT_STORAGE_ENDPOINT:-}"
if [ "$DRY_RUN" = "true" ]; then
  info "[DRY RUN] Would verify object storage reachability"
else
  [ -z "$OBJ" ] && OBJ=$(kubectl get secret -n storage seaweedfs-s3-config -o jsonpath='{.data.endpoint}' 2>/dev/null | base64 -d 2>/dev/null || echo "")
  [ -n "$OBJ" ] || { error "Object storage endpoint is unavailable"; exit 1; }
  curl -sS --max-time 5 -o /dev/null "$OBJ" || { error "Object storage is unreachable: $OBJ"; exit 1; }
  info "Storage reachable"
fi
TS=$(date -u +%Y%m%dT%H%M%SZ); RF="${PROJECT_ROOT}/.backup-results-${TS}.log"
TOTAL=0; PASSED=0; FAILED=0; SKIPPED=0
check_comp() { kubectl get pods -n "$1" -l "$2" -o name 2>/dev/null | grep -q .; }
run_backup() {
  local comp="$1" cronjob="$2" ns="$3" lbl="$4"; TOTAL=$((TOTAL+1))
  if dry_run_component_is_disabled "$comp"; then
    info "[DRY RUN] Would skip disabled component ${comp}"
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"; return 0
  fi
  if [ "$DRY_RUN" = "true" ]; then info "[DRY RUN] Would trigger ${ns}/${cronjob}"; PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"; return 0; fi
  if ! check_comp "$ns" "$lbl"; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested component '${comp}' is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
    else
      warn "'${comp}' not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
    fi
    return 0
  fi
  if ! kubectl get cronjob "$cronjob" -n "$ns" &>/dev/null; then
    error "Backup CronJob ${ns}/${cronjob} is missing; deploy the backup-restore role first"
    FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"; return 0
  fi
  local job_suffix job
  job_suffix=$(printf '%s' "$TS" | tr '[:upper:]' '[:lower:]')
  job="${cronjob}-manual-${job_suffix}"
  job="${job//:/-}"
  if kubectl create job "$job" --from="cronjob/${cronjob}" -n "$ns" 2>&1 | tee -a "$RF" &&
     kubectl wait --for=condition=complete "job/${job}" -n "$ns" --timeout=3600s 2>&1 | tee -a "$RF"; then
    PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"
  else
    kubectl logs "job/${job}" -n "$ns" --all-containers --tail=200 2>&1 | tee -a "$RF" || true
    FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
  fi
}
echo "Timestamp: ${TS}" > "$RF"
run_mongodb_backup() {
  local comp=mongodb ns=databases cluster="${PROJECT_NAME:-k8s}-mongo"
  local backup
  backup="${cluster}-manual-$(printf '%s' "$TS" | tr '[:upper:]' '[:lower:]')"
  TOTAL=$((TOTAL+1))
  if dry_run_component_is_disabled "$comp"; then
    info "[DRY RUN] Would skip disabled component ${comp}"
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"; return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would create PerconaServerMongoDBBackup ${ns}/${backup}"
    PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"; return 0
  fi
  if ! kubectl get perconaservermongodb "$cluster" -n "$ns" >/dev/null 2>&1; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested MongoDB cluster ${ns}/${cluster} is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
    else
      warn "MongoDB is not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
    fi
    return 0
  fi
  kubectl apply -f - <<EOF | tee -a "$RF"
apiVersion: psmdb.percona.com/v1
kind: PerconaServerMongoDBBackup
metadata:
  name: ${backup}
  namespace: ${ns}
spec:
  clusterName: ${cluster}
  storageName: s3-object-storage
EOF
  local state attempts=0
  while [ "$attempts" -lt 360 ]; do
    state=$(kubectl get perconaservermongodbbackup "$backup" -n "$ns" -o jsonpath='{.status.state}' 2>/dev/null || echo pending)
    case "${state,,}" in
      ready|successful) PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"; return 0 ;;
      error|failed) break ;;
    esac
    attempts=$((attempts+1)); sleep 10
  done
  kubectl describe perconaservermongodbbackup "$backup" -n "$ns" | tee -a "$RF"
  error "MongoDB backup ${ns}/${backup} failed or timed out"
  FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
}
run_postgresql_backup() {
  local comp=postgresql ns=databases cluster="${PROJECT_NAME:-k8s}-pg"
  local backup state attempts=0
  backup="${cluster}-manual-$(printf '%s' "$TS" | tr '[:upper:]' '[:lower:]')"
  TOTAL=$((TOTAL+1))
  if dry_run_component_is_disabled "$comp"; then
    info "[DRY RUN] Would skip disabled component ${comp}"
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"; return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would create PerconaPGBackup ${ns}/${backup}"
    PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"; return 0
  fi
  if ! kubectl get perconapgcluster "$cluster" -n "$ns" >/dev/null 2>&1; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested PostgreSQL cluster ${ns}/${cluster} is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
    else
      warn "PostgreSQL is not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
    fi
    return 0
  fi
  kubectl apply -f - <<EOF | tee -a "$RF"
apiVersion: pgv2.percona.com/v2
kind: PerconaPGBackup
metadata:
  name: ${backup}
  namespace: ${ns}
spec:
  pgCluster: ${cluster}
  repoName: repo1
  options:
    - --type=full
EOF
  while [ "$attempts" -lt 360 ]; do
    state=$(kubectl get perconapgbackup "$backup" -n "$ns" -o jsonpath='{.status.state}' 2>/dev/null || echo pending)
    case "${state,,}" in
      succeeded|successful|ready) PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"; return 0 ;;
      failed|error) break ;;
    esac
    attempts=$((attempts+1)); sleep 10
  done
  kubectl describe perconapgbackup "$backup" -n "$ns" | tee -a "$RF" || true
  error "PostgreSQL backup ${ns}/${backup} failed or timed out"
  FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
}
run_named_backup() {
  case "$1" in
    postgresql) run_postgresql_backup ;;
    mongodb)   run_mongodb_backup ;;
    vault)     run_backup vault vault-raft-snapshot vault app.kubernetes.io/name=vault ;;
    seaweedfs) run_backup seaweedfs seaweedfs-backup-check storage app.kubernetes.io/name=seaweedfs ;;
    gitlab)
      run_backup gitlab gitlab-toolbox-backup gitlab 'release=gitlab,app=toolbox'
      run_backup gitlab-secrets gitlab-rails-secrets-backup gitlab 'release=gitlab,app=toolbox'
      ;;
    *) error "Unknown component: $1"; return 1 ;;
  esac
}
if [ "$COMPONENT" = "all" ] || [ -z "$COMPONENT" ]; then
  for c in postgresql mongodb vault seaweedfs gitlab; do run_named_backup "$c"; done
else
  run_named_backup "$COMPONENT"
fi
echo "============================================"; echo "  BACKUP SUMMARY"; echo "============================================"
echo "  Total: ${TOTAL}  Passed: ${PASSED}  Failed: ${FAILED}  Skipped: ${SKIPPED}"
[ "$FAILED" -gt 0 ] && error "Failed." && exit 1
info "Completed successfully."; exit 0
