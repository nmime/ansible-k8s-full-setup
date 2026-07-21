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
DRY_RUN=false; COMPONENT=""; FORCE=false; CONFIG_FILE=""; RESULT_JSON=""
[[ ! -f "${PROJECT_ROOT}/platform-orchestrator/platform.yaml" ]] \
  || CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)   DRY_RUN=true; shift ;;
    --component) COMPONENT="$2"; shift 2 ;;
    --config)    CONFIG_FILE="$2"; shift 2 ;;
    --result-json) RESULT_JSON="$2"; shift 2 ;;
    --force)     FORCE=true; shift ;;
    -h|--help) echo "Usage: $0 [--dry-run] [--component <name>] [--config FILE] [--result-json FILE] [--force]";
               echo "  --component  postgresql|mongodb|vault|seaweedfs|gitlab|all"; exit 0 ;;
    *) error "Unknown option: $1"; exit 1 ;;
  esac
done
if [ -n "$CONFIG_FILE" ]; then
  [ -f "$CONFIG_FILE" ] || { error "Platform config not found: $CONFIG_FILE"; exit 1; }
  command -v yq >/dev/null 2>&1 || { error "yq is required with --config"; exit 1; }
  if [ -z "${PROJECT_NAME:-}" ]; then
    PROJECT_NAME=$(yq -r '.global.project // ""' "$CONFIG_FILE")
    [ -n "$PROJECT_NAME" ] \
      || { error "global.project is required in $CONFIG_FILE"; exit 1; }
    export PROJECT_NAME
  fi
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
  if [ -z "$OBJ" ] && kubectl get service seaweedfs-filer -n storage >/dev/null 2>&1; then
    OBJ=http://seaweedfs-filer.storage.svc.cluster.local:8333
  fi
  [ -n "$OBJ" ] || { error "Object storage endpoint is unavailable"; exit 1; }
  if [[ "$OBJ" == *".svc"* || "$OBJ" == *".svc."* ]]; then
    probe="backup-storage-probe-$(date +%s)"
    probe_overrides=$(jq -cn --arg name "$probe" --arg url "$OBJ" '{spec:{securityContext:{runAsNonRoot:true,seccompProfile:{type:"RuntimeDefault"}},containers:[{name:$name,image:"curlimages/curl:8.17.0",command:["curl","-sS","--max-time","10","-o","/dev/null",$url],securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]},runAsNonRoot:true,runAsUser:100,runAsGroup:1000}}]}}')
    kubectl run "$probe" --namespace storage --rm -i --restart=Never \
      --image=curlimages/curl:8.17.0 --overrides="$probe_overrides" \
      || { error "Object storage is unreachable from the cluster: $OBJ"; exit 1; }
  else
    curl -sS --max-time 5 -o /dev/null "$OBJ" || { error "Object storage is unreachable: $OBJ"; exit 1; }
  fi
  info "Storage reachable"
fi
TS=$(date -u +%Y%m%dT%H%M%SZ)
# Multi-cluster controllers can start several backups in the same second.
# Keep each audit trail independent even for two runs of the same project.
RESULT_PROJECT=$(printf '%s' "${PROJECT_NAME:-k8s}" | tr -c 'A-Za-z0-9._-' '-')
RF="${PROJECT_ROOT}/.backup-results-${RESULT_PROJECT}-${TS}-$$.log"
CATALOG_RECORDS=$(mktemp "${TMPDIR:-/tmp}/native-backup-catalog.XXXXXX")
# Invoked indirectly by the EXIT/INT/TERM trap below.
# shellcheck disable=SC2329
cleanup_catalog() { rm -f "$CATALOG_RECORDS"; }
trap cleanup_catalog EXIT INT TERM
catalog_record() {
  local component="$1" namespace="$2" kind="$3" name="$4" state="$5"
  local contract="$6" locator="${7:-}" repository="${8:-}"
  jq -cn --arg component "$component" --arg namespace "$namespace" \
    --arg kind "$kind" --arg name "$name" --arg state "$state" \
    --arg contract "$contract" --arg locator "$locator" \
    --arg repository "$repository" --arg recordedAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    '{component:$component,namespace:$namespace,kind:$kind,name:$name,state:$state,
      restore_contract:$contract,artifact_locator:$locator,repository:$repository,
      recorded_at:$recordedAt}' >> "$CATALOG_RECORDS"
}
write_catalog() {
  [[ -n "$RESULT_JSON" ]] || return 0
  mkdir -p "$(dirname "$RESULT_JSON")"
  jq -s --arg project "${PROJECT_NAME:-k8s}" --arg createdAt "$TS" \
    --argjson expected "$TOTAL" --argjson passed "$PASSED" \
    --argjson failed "$FAILED" --argjson skipped "$SKIPPED" \
    '{schema_version:1,project:$project,created_at:$createdAt,
      summary:{expected:$expected,passed:$passed,failed:$failed,skipped:$skipped},
      completeness:(if length == $expected and $failed == 0 and
        all(.[]; .state == "completed" or .state == "velero-fallback" or .state == "disabled")
        then "complete" else "incomplete" end),
      artifacts:.}' "$CATALOG_RECORDS" > "${RESULT_JSON}.tmp"
  mv "${RESULT_JSON}.tmp" "$RESULT_JSON"
  chmod 600 "$RESULT_JSON"
}
TOTAL=0; PASSED=0; FAILED=0; SKIPPED=0
check_comp() {
  kubectl get pods -n "$1" -l "$2" -o json 2>/dev/null \
    | jq -e '.items | length > 0' >/dev/null
}
wait_for_job() {
  local ns="$1" job="$2" deadline phase
  deadline=$((SECONDS + 3600))
  while (( SECONDS < deadline )); do
    phase=$(kubectl get job "$job" -n "$ns" -o json 2>/dev/null | jq -r '
      if any(.status.conditions[]?; .type == "Complete" and .status == "True") then "complete"
      elif any(.status.conditions[]?; .type == "Failed" and .status == "True") then "failed"
      else "running" end' 2>/dev/null || echo missing)
    case "$phase" in
      complete) return 0 ;;
      failed|missing) return 1 ;;
    esac
    sleep 5
  done
  return 1
}
run_backup() {
  local comp="$1" cronjob="$2" ns="$3" lbl="$4"; TOTAL=$((TOTAL+1))
  if dry_run_component_is_disabled "$comp"; then
    info "[DRY RUN] Would skip disabled component ${comp}"
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"
    catalog_record "$comp" "$ns" CronJob "$cronjob" disabled disabled
    return 0
  fi
  if [ "$DRY_RUN" = "true" ]; then info "[DRY RUN] Would trigger ${ns}/${cronjob}"; PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"; catalog_record "$comp" "$ns" CronJob "$cronjob" planned planned; return 0; fi
  if ! check_comp "$ns" "$lbl"; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested component '${comp}' is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
      catalog_record "$comp" "$ns" CronJob "$cronjob" failed missing-workload
    else
      warn "'${comp}' not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
      catalog_record "$comp" "$ns" CronJob "$cronjob" disabled disabled
    fi
    return 0
  fi
  if ! kubectl get cronjob "$cronjob" -n "$ns" &>/dev/null; then
    error "Backup CronJob ${ns}/${cronjob} is missing; deploy the backup-restore role first"
    FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
    catalog_record "$comp" "$ns" CronJob "$cronjob" failed missing-cronjob
    return 0
  fi
  local job_suffix job
  job_suffix=$(printf '%s' "$TS" | tr '[:upper:]' '[:lower:]')
  job="${cronjob}-manual-${job_suffix}"
  job="${job//:/-}"
  if kubectl create job "$job" --from="cronjob/${cronjob}" -n "$ns" 2>&1 | tee -a "$RF" &&
     wait_for_job "$ns" "$job"; then
    if [ "$comp" = gitlab ]; then
      local gitlab_logs
      gitlab_logs=$(kubectl logs "job/${job}" -n "$ns" --all-containers 2>&1)
      printf '%s\n' "$gitlab_logs" >> "$RF"
      if printf '%s\n' "$gitlab_logs" | grep -Eq 'Unable to check existence of bucket|Skipping backup of (registry|uploads|artifacts|lfs|packages|external_diffs|terraform_state|pages|ci_secure_files|agent_plan_content)'; then
        error "GitLab Toolbox completed after skipping one or more required object-storage components"
        FAILED=$((FAILED+1)); echo "${comp}: FAIL-incomplete" >> "$RF"
        return 0
      fi
    fi
    PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"
    case "$comp" in
      vault) catalog_record "$comp" "$ns" Job "$job" completed vault-raft "s3://backups/${PROJECT_NAME:-k8s}/vault/" ;;
      seaweedfs) catalog_record "$comp" "$ns" Job "$job" completed seaweedfs-topology "s3://backups/${PROJECT_NAME:-k8s}/seaweedfs/" ;;
      gitlab) catalog_record "$comp" "$ns" Job "$job" completed gitlab-toolbox "s3://gitlab-backups/" ;;
      gitlab-secrets) catalog_record "$comp" "$ns" Job "$job" completed gitlab-rails-secrets "s3://backups/${PROJECT_NAME:-k8s}/gitlab-secrets/" ;;
    esac
  else
    kubectl logs "job/${job}" -n "$ns" --all-containers --tail=200 2>&1 | tee -a "$RF" || true
    FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
    catalog_record "$comp" "$ns" Job "$job" failed job
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
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"
    catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" disabled disabled
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would create PerconaServerMongoDBBackup ${ns}/${backup}"
    PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"
    catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" planned pbm "s3-object-storage"
    return 0
  fi
  if ! kubectl get perconaservermongodb "$cluster" -n "$ns" >/dev/null 2>&1; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested MongoDB cluster ${ns}/${cluster} is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
      catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" failed missing-cluster
    else
      warn "MongoDB is not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
      catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" disabled disabled
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
  local state state_lower attempts=0
  while [ "$attempts" -lt 360 ]; do
    state=$(kubectl get perconaservermongodbbackup "$backup" -n "$ns" -o jsonpath='{.status.state}' 2>/dev/null || echo pending)
    state_lower=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
    case "$state_lower" in
      ready|successful) PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"; catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" completed pbm "s3-object-storage"; return 0 ;;
      error|failed) break ;;
    esac
    attempts=$((attempts+1)); sleep 10
  done
  kubectl describe perconaservermongodbbackup "$backup" -n "$ns" | tee -a "$RF"
  error "MongoDB backup ${ns}/${backup} failed or timed out"
  FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
  catalog_record "$comp" "$ns" PerconaServerMongoDBBackup "$backup" failed pbm "s3-object-storage"
}
run_postgresql_backup() {
  local comp=postgresql ns=databases cluster="${PROJECT_NAME:-k8s}-pg"
  local backup state state_lower job_name backup_set attempts=0 repo="${BACKUP_POSTGRESQL_REPO:-repo2}"
  local timeout_seconds="${BACKUP_POSTGRESQL_TIMEOUT_SECONDS:-1800}" max_attempts
  [[ "$timeout_seconds" =~ ^[0-9]+$ ]] && [ "$timeout_seconds" -ge 10 ] \
    || { error "BACKUP_POSTGRESQL_TIMEOUT_SECONDS must be an integer >= 10"; FAILED=$((FAILED+1)); return 0; }
  max_attempts=$((timeout_seconds / 10))
  backup="${cluster}-manual-$(printf '%s' "$TS" | tr '[:upper:]' '[:lower:]')"
  TOTAL=$((TOTAL+1))
  if dry_run_component_is_disabled "$comp"; then
    info "[DRY RUN] Would skip disabled component ${comp}"
    SKIPPED=$((SKIPPED+1)); echo "${comp}: DRY-SKIP" >> "$RF"
    catalog_record "$comp" "$ns" PerconaPGBackup "$backup" disabled disabled "" "$repo"
    return 0
  fi
  if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would create PerconaPGBackup ${ns}/${backup}"
    PASSED=$((PASSED+1)); echo "${comp}: DRY-OK" >> "$RF"
    catalog_record "$comp" "$ns" PerconaPGBackup "$backup" planned pgbackrest "" "$repo"
    return 0
  fi
  if ! kubectl get perconapgcluster "$cluster" -n "$ns" >/dev/null 2>&1; then
    if { [ -n "$COMPONENT" ] && [ "$COMPONENT" != all ]; } || component_expected "$comp"; then
      error "Requested PostgreSQL cluster ${ns}/${cluster} is not deployed"
      FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
      catalog_record "$comp" "$ns" PerconaPGBackup "$backup" failed missing-cluster "" "$repo"
    else
      warn "PostgreSQL is not deployed"
      SKIPPED=$((SKIPPED+1)); echo "${comp}: SKIP" >> "$RF"
      catalog_record "$comp" "$ns" PerconaPGBackup "$backup" disabled disabled "" "$repo"
    fi
    return 0
  fi
  if ! kubectl get perconapgcluster "$cluster" -n "$ns" -o json \
    | jq -e --arg repo "$repo" '.spec.backups.pgbackrest.repos | any(.name == $repo)' >/dev/null; then
    error "PostgreSQL backup repository ${repo} is not configured on ${ns}/${cluster}"
    FAILED=$((FAILED+1)); echo "${comp}: FAIL-missing-${repo}" >> "$RF"
    catalog_record "$comp" "$ns" PerconaPGBackup "$backup" failed missing-repository "" "$repo"
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
  repoName: ${repo}
  options:
    - --type=full
EOF
  while [ "$attempts" -lt "$max_attempts" ]; do
    state=$(kubectl get perconapgbackup "$backup" -n "$ns" -o jsonpath='{.status.state}' 2>/dev/null || echo pending)
    state_lower=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
    case "$state_lower" in
      succeeded|successful|ready)
        backup_set=$(kubectl get perconapgbackup "$backup" -n "$ns" \
          -o jsonpath='{.status.backupName}' 2>/dev/null || true)
        [[ -n "$backup_set" ]] || break
        PASSED=$((PASSED+1)); echo "${comp}: PASS" >> "$RF"
        catalog_record "$comp" "$ns" PerconaPGBackup "$backup" completed pgbackrest "$backup_set" "$repo"
        return 0
        ;;
      failed|error) break ;;
    esac
    # Percona 3.x can leave the CR at Running after its backing Job has
    # exhausted retries. Observe the Job directly so a terminal failure does
    # not turn into an hour-long false wait.
    job_name=$(kubectl get perconapgbackup "$backup" -n "$ns" \
      -o jsonpath='{.status.jobName}' 2>/dev/null || true)
    if [ -n "$job_name" ] && kubectl get job "$job_name" -n "$ns" -o json 2>/dev/null \
      | jq -e 'any(.status.conditions[]?; .type == "Failed" and .status == "True")' >/dev/null; then
      state=failed
      break
    fi
    attempts=$((attempts+1)); sleep 10
  done
  if [ -n "${job_name:-}" ]; then
    kubectl logs "job/${job_name}" -n "$ns" --all-containers --tail=200 2>&1 | tee -a "$RF" || true
  fi
  kubectl describe perconapgbackup "$backup" -n "$ns" | tee -a "$RF" || true
  error "PostgreSQL backup ${ns}/${backup} failed or timed out"
  FAILED=$((FAILED+1)); echo "${comp}: FAIL" >> "$RF"
  catalog_record "$comp" "$ns" PerconaPGBackup "$backup" failed pgbackrest "" "$repo"
}
run_named_backup() {
  case "$1" in
    postgresql) run_postgresql_backup ;;
    mongodb)   run_mongodb_backup ;;
    vault)
      if [[ "$DRY_RUN" == true ]]; then
        run_backup vault vault-raft-snapshot vault app.kubernetes.io/name=vault
      else
        vault_storage=$(kubectl exec -n vault vault-0 -- vault status -format=json 2>/dev/null | jq -r '.storage_type // "unknown"' 2>/dev/null || echo unknown)
        if [[ "$vault_storage" == raft ]]; then
          run_backup vault vault-raft-snapshot vault app.kubernetes.io/name=vault
        elif [[ "$vault_storage" == file && "${BACKUP_ALLOW_VELERO_VAULT_FALLBACK:-false}" == true ]]; then
          TOTAL=$((TOTAL+1)); SKIPPED=$((SKIPPED+1))
          warn "Vault uses file storage; native Raft snapshot deferred to the mandatory Velero filesystem backup"
          echo "vault: VELERO-FALLBACK" >> "$RF"
          catalog_record vault vault PersistentVolumeClaim vault-data velero-fallback velero-filesystem
        else
          TOTAL=$((TOTAL+1)); FAILED=$((FAILED+1))
          error "Vault native backup requires integrated Raft storage (detected: ${vault_storage})"
          echo "vault: FAIL-${vault_storage}" >> "$RF"
          catalog_record vault vault StatefulSet vault failed unsupported-storage
        fi
      fi
      ;;
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
write_catalog
[ "$FAILED" -gt 0 ] && error "Failed." && exit 1
info "Completed successfully."; exit 0
