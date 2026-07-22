#!/usr/bin/env bash
# Replay application-consistent native backups into a replacement cluster.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"

CATALOG=""
RECEIPT=""
ARCHIVE=""
CONFIG=""
STATE_FILE=""
VAULT_INIT_FILE=""
MODE=plan
CONFIRM=""
RESUME=false
TIMEOUT_SECONDS="${NATIVE_RESTORE_TIMEOUT_SECONDS:-3600}"
WORK_DIR=""
LOCK_DIR=""
TEMP_VAULT_SECRET=""
GITLAB_REPLICAS_FILE=""
GITLAB_SCALED=false

log() { printf '[native-restore] %s\n' "$*"; }
fail() { printf '[native-restore] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage: native-restore.sh --catalog FILE --receipt FILE --archive FILE
       --config FILE --state-file FILE [OPTIONS]

Options:
  --mode plan|execute      plan is read-only (default)
  --confirm PHRASE        execute requires the exact phrase printed by plan
  --resume                resume the same target-bound operation
  --vault-init-file FILE  Ansible-Vault-encrypted Vault init material
  --timeout-seconds N     component timeout (default: 3600)
  -h, --help              show this help

The exact confirmation is RESTORE_NATIVE_<project>_<backup-id>_<target-uid>.
Legacy catalogs, "latest" locators, source-cluster targets, incomplete receipts,
and enabled technologies without a completed exact artifact fail closed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --catalog) CATALOG="${2:-}"; shift 2 ;;
    --receipt) RECEIPT="${2:-}"; shift 2 ;;
    --archive) ARCHIVE="${2:-}"; shift 2 ;;
    --config) CONFIG="${2:-}"; shift 2 ;;
    --state-file) STATE_FILE="${2:-}"; shift 2 ;;
    --vault-init-file) VAULT_INIT_FILE="${2:-}"; shift 2 ;;
    --mode) MODE="${2:-}"; shift 2 ;;
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --resume) RESUME=true; shift ;;
    --timeout-seconds) TIMEOUT_SECONDS="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ "$MODE" == plan || "$MODE" == execute ]] || fail "--mode must be plan or execute"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || fail "--timeout-seconds must be positive"
for required in "$CATALOG" "$RECEIPT" "$ARCHIVE" "$CONFIG"; do
  [[ -f "$required" && ! -L "$required" ]] || fail "required regular file is missing: $required"
done
[[ -n "$STATE_FILE" ]] || fail "--state-file is required"
[[ ! -L "$STATE_FILE" ]] || fail "state file must not be a symlink"
for command_name in jq yq kubectl; do
  command -v "$command_name" >/dev/null 2>&1 || fail "$command_name is required"
done

sha256_file() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else shasum -a 256 "$1" | awk '{print $1}'; fi
}
short_hash() { printf '%s' "$1" | { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; } | awk '{print substr($1,1,12)}'; }
ARCHIVE_SHA=$(sha256_file "$ARCHIVE")
CATALOG_SHA=$(sha256_file "$CATALOG")
PROJECT=$(jq -r '.project // ""' "$CATALOG")
BACKUP_ID=$(jq -r '.backup_id // ""' "$CATALOG")
SOURCE_UID=$(jq -r '.source_cluster_uid // ""' "$RECEIPT")
TARGET_UID=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')
TARGET_CONTEXT=$(kubectl config current-context)

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9-]{1,47}$ ]] || fail "catalog project is unsafe"
[[ "$BACKUP_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$ ]] || fail "catalog backup_id is unsafe"
[[ -n "$SOURCE_UID" && -n "$TARGET_UID" && "$SOURCE_UID" != "$TARGET_UID" ]] \
  || fail "native replay requires a replacement cluster UID"
[[ "$(yq -r '.global.project // ""' "$CONFIG")" == "$PROJECT" ]] \
  || fail "config project does not match catalog"

jq -e --arg project "$PROJECT" --arg backupId "$BACKUP_ID" \
  --arg sourceUid "$SOURCE_UID" --arg archive "$(basename "$ARCHIVE")" \
  --arg sha "$ARCHIVE_SHA" --arg catalogSha "$CATALOG_SHA" '
    .schema_version == 2 and .receipt_type == "encrypted-cluster-backup" and
    .project == $project and .backup_id == $backupId and
    .source_cluster_uid == $sourceUid and .archive == $archive and .sha256 == $sha and
    .native_backup_catalog_sha256 == $catalogSha and
    .completeness == "complete" and .remote.published == true and
    .remote.download_sha256_verified == true and
    .remote.receipt_uploaded_last == true and .remote.publication_state == "complete"
  ' "$RECEIPT" >/dev/null || fail "schema-v2 receipt is incomplete or not bound to the archive/catalog"

EXPECTED_ORDER='["seaweedfs","vault","postgresql","mongodb","gitlab-secrets","gitlab"]'
jq -e --arg project "$PROJECT" --arg backupId "$BACKUP_ID" \
  --argjson order "$EXPECTED_ORDER" '
    .schema_version == 2 and .project == $project and .backup_id == $backupId and
    .completeness == "complete" and .summary.failed == 0 and
    .summary.expected == (.artifacts | length) and .restore_order == $order and
    ([.artifacts[].component] | length) == ([.artifacts[].component] | unique | length) and
    all(.artifacts[];
      (.state == "completed" and
       (.artifact_locator | type == "string" and length > 0) and
       (.artifact_locator | ascii_downcase | contains("latest") | not)) or
      .state == "velero-fallback" or .state == "disabled")
  ' "$CATALOG" >/dev/null || fail "native catalog is not a complete schema-v2 exact-artifact catalog"

component_enabled() {
  case "$1" in
    postgresql) [[ $(yq -r '(.databases.enabled and .databases.postgresql.enabled) // false' "$CONFIG") == true ]] ;;
    mongodb) [[ $(yq -r '(.databases.enabled and .databases.mongodb.enabled) // false' "$CONFIG") == true ]] ;;
    vault) [[ $(yq -r '.secrets.enabled // false' "$CONFIG") == true ]] ;;
    seaweedfs) [[ $(yq -r '.storage.enabled // false' "$CONFIG") == true ]] ;;
    gitlab|gitlab-secrets) [[ $(yq -r '.gitlab.enabled // false' "$CONFIG") == true ]] ;;
    *) return 1 ;;
  esac
}

artifact_json() {
  jq -ec --arg component "$1" '[.artifacts[] | select(.component == $component)] |
    if length == 1 then .[0] else error("component cardinality") end' "$CATALOG"
}

for component in seaweedfs vault postgresql mongodb gitlab-secrets gitlab; do
  artifact=$(artifact_json "$component") || fail "catalog must contain exactly one $component record"
  state=$(jq -r '.state' <<<"$artifact")
  if component_enabled "$component"; then
    if [[ "$component" == vault && "$state" == velero-fallback ]]; then
      :
    else
      [[ "$state" == completed ]] || fail "enabled component $component has no completed native artifact"
    fi
  else
    [[ "$state" == disabled ]] || fail "disabled component $component is not catalogued as disabled"
  fi
done

CONFIRMATION="RESTORE_NATIVE_${PROJECT}_${BACKUP_ID}_${TARGET_UID}"
if [[ "$MODE" == plan ]]; then
  jq -n --arg project "$PROJECT" --arg backupId "$BACKUP_ID" \
    --arg sourceUid "$SOURCE_UID" --arg targetUid "$TARGET_UID" \
    --arg context "$TARGET_CONTEXT" --arg confirmation "$CONFIRMATION" \
    --argjson order "$EXPECTED_ORDER" \
    '{mode:"plan",project:$project,backup_id:$backupId,source_cluster_uid:$sourceUid,
      target_cluster_uid:$targetUid,target_context:$context,restore_order:$order,
      required_confirmation:$confirmation}'
  exit 0
fi

[[ "$CONFIRM" == "$CONFIRMATION" ]] || fail "destructive confirmation mismatch; run --mode plan"
STATE_PARENT=$(dirname "$STATE_FILE")
mkdir -p "$STATE_PARENT"
chmod 700 "$STATE_PARENT"
LOCK_DIR="${STATE_FILE}.lock"
mkdir "$LOCK_DIR" 2>/dev/null || fail "another native restore owns lock $LOCK_DIR"
WORK_DIR=$(mktemp -d "${STATE_PARENT}/.native-restore.XXXXXX")
chmod 700 "$WORK_DIR"
cleanup() {
  local rc=$?
  trap - EXIT INT TERM
  if [[ "$GITLAB_SCALED" == true && -f "$GITLAB_REPLICAS_FILE" ]]; then
    while IFS=$'\t' read -r deployment replicas; do
      [[ -z "$deployment" ]] || kubectl scale deployment "$deployment" -n gitlab \
        --replicas="$replicas" >/dev/null 2>&1 || true
    done < <(jq -r '.[] | [.name,.replicas] | @tsv' "$GITLAB_REPLICAS_FILE")
  fi
  if [[ -n "$TEMP_VAULT_SECRET" ]]; then
    kubectl delete secret "$TEMP_VAULT_SECRET" -n vault --ignore-not-found --wait=false >/dev/null 2>&1 || true
  fi
  [[ -z "$WORK_DIR" ]] || rm -rf "$WORK_DIR"
  [[ -z "$LOCK_DIR" ]] || rmdir "$LOCK_DIR" 2>/dev/null || true
  exit "$rc"
}
trap cleanup EXIT INT TERM

write_state() {
  local tmp="${STATE_FILE}.tmp"
  jq "$@" "$STATE_FILE" > "$tmp"
  chmod 600 "$tmp"
  mv "$tmp" "$STATE_FILE"
}

if [[ -e "$STATE_FILE" ]]; then
  [[ "$RESUME" == true ]] || fail "state exists; use --resume after inspection: $STATE_FILE"
  jq -e --arg project "$PROJECT" --arg backupId "$BACKUP_ID" \
    --arg archiveSha "$ARCHIVE_SHA" --arg catalogSha "$CATALOG_SHA" \
    --arg sourceUid "$SOURCE_UID" --arg targetUid "$TARGET_UID" '
      .schema_version == 1 and .project == $project and .backup_id == $backupId and
      .archive_sha256 == $archiveSha and .catalog_sha256 == $catalogSha and
      .source_cluster_uid == $sourceUid and .target_cluster_uid == $targetUid
    ' "$STATE_FILE" >/dev/null || fail "resume state is not bound to these exact inputs and target"
else
  [[ "$RESUME" == false ]] || fail "--resume requested but state file does not exist"
  jq -n --arg project "$PROJECT" --arg backupId "$BACKUP_ID" \
    --arg archiveSha "$ARCHIVE_SHA" --arg catalogSha "$CATALOG_SHA" \
    --arg sourceUid "$SOURCE_UID" --arg targetUid "$TARGET_UID" \
    --arg context "$TARGET_CONTEXT" --arg createdAt "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    --argjson order "$EXPECTED_ORDER" '
      {schema_version:1,project:$project,backup_id:$backupId,
       archive_sha256:$archiveSha,catalog_sha256:$catalogSha,
       source_cluster_uid:$sourceUid,target_cluster_uid:$targetUid,target_context:$context,
       created_at:$createdAt,updated_at:$createdAt,status:"running",restore_order:$order,
       components:($order | map({key:.,value:{state:"pending"}}) | from_entries)}
    ' > "$STATE_FILE"
  chmod 600 "$STATE_FILE"
fi

checkpoint() {
  local component="$1" state="$2" detail="${3:-}" now
  now=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  # jq variables are expanded by jq, not by the shell.
  # shellcheck disable=SC2016
  write_state --arg component "$component" --arg state "$state" --arg detail "$detail" \
    --arg now "$now" '.components[$component]={state:$state,detail:$detail,updated_at:$now} | .updated_at=$now'
}

component_done() { [[ $(jq -r --arg c "$1" '.components[$c].state' "$STATE_FILE") == completed ]]; }
wait_json_state() {
  local resource="$1" name="$2" namespace="$3" desired="$4" deadline state
  deadline=$((SECONDS + TIMEOUT_SECONDS))
  while (( SECONDS < deadline )); do
    state=$(kubectl get "$resource" "$name" -n "$namespace" -o jsonpath='{.status.state}' 2>/dev/null || true)
    state=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
    [[ "$state" == "$desired" ]] && return 0
    [[ "$state" == failed || "$state" == error ]] && return 1
    sleep 10
  done
  return 1
}

parse_s3_uri() {
  local uri="$1"
  [[ "$uri" =~ ^s3://([^/]+)/(.+)$ ]] || return 1
  S3_BUCKET="${BASH_REMATCH[1]}"
  S3_KEY="${BASH_REMATCH[2]}"
  [[ "$S3_BUCKET" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,62}$ ]] || return 1
  [[ "$S3_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._/+=:@-]{0,1023}$ ]] || return 1
}

head_s3_object() {
  local component="$1" namespace="$2" secret="$3" uri="$4"
  local pod
  pod="native-head-${component}-$(short_hash "$BACKUP_ID")"
  parse_s3_uri "$uri" || fail "$component artifact is not an exact S3 URI"
  kubectl delete pod "$pod" -n "$namespace" --ignore-not-found --wait=true >/dev/null
  kubectl apply -n "$namespace" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  labels: {backup-restore.io/native-restore: "true"}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 300
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: head
      image: amazon/aws-cli:2.34.48
      command: [/bin/sh, -ec]
      args: ['aws --endpoint-url="\$AWS_ENDPOINT_URL" s3api head-object --bucket "${S3_BUCKET}" --key "${S3_KEY}" >/dev/null']
      envFrom:
        - secretRef: {name: ${secret}}
      env: [{name: HOME, value: /tmp}]
      resources:
        requests: {cpu: 25m, memory: 64Mi}
        limits: {cpu: 200m, memory: 256Mi}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
EOF
  kubectl wait "pod/$pod" -n "$namespace" --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null \
    || { kubectl logs "pod/$pod" -n "$namespace" --tail=50 >&2 || true; fail "$component exact object is unavailable"; }
  kubectl delete pod "$pod" -n "$namespace" --wait=true >/dev/null
}

verify_seaweedfs_topology() {
  local uri="$1" pod
  parse_s3_uri "$uri" || fail "SeaweedFS topology artifact is not an exact S3 URI"
  pod="native-seaweed-topology-$(short_hash "$BACKUP_ID")"
  kubectl delete pod "$pod" -n storage --ignore-not-found --wait=true >/dev/null
  kubectl apply -n storage -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: ${pod}
  labels: {backup-restore.io/native-restore: "true"}
spec:
  restartPolicy: Never
  activeDeadlineSeconds: 600
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
  volumes: [{name: work, emptyDir: {sizeLimit: 16Mi}}]
  initContainers:
    - name: download
      image: amazon/aws-cli:2.34.48
      command: [/bin/sh, -ec]
      args: ['aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 cp "s3://${S3_BUCKET}/${S3_KEY}" /work/source.json --only-show-errors; test -s /work/source.json']
      envFrom: [{secretRef: {name: seaweedfs-backup-credentials}}]
      env: [{name: HOME, value: /tmp}]
      volumeMounts: [{name: work, mountPath: /work}]
      resources:
        requests: {cpu: 25m, memory: 64Mi}
        limits: {cpu: 200m, memory: 256Mi}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
  containers:
    - name: compare
      image: python:3.14-alpine3.22
      command: [python3, -c]
      args:
        - |
          import json, urllib.request
          source=json.load(open('/work/source.json', encoding='utf-8'))
          with urllib.request.urlopen('http://seaweedfs-master.storage.svc.cluster.local:9333/dir/status', timeout=20) as response:
              current=json.load(response)
          if not isinstance(source, dict) or not source or not isinstance(current, dict) or not current:
              raise SystemExit('source/current topology is not a non-empty JSON object')
          identity_keys={'id','volumeid','volume_id','url','publicurl'}
          def identities(value):
              found=set()
              if isinstance(value, dict):
                  for key, child in value.items():
                      if key.lower() in identity_keys and isinstance(child, (str,int)):
                          found.add(f'{key.lower()}={child}')
                      found.update(identities(child))
              elif isinstance(value, list):
                  for child in value: found.update(identities(child))
              return found
          source_ids=identities(source); current_ids=identities(current)
          if not source_ids:
              raise SystemExit('backup topology contains no stable node/volume identities')
          missing=sorted(source_ids-current_ids)
          if missing:
              raise SystemExit('recovered topology is missing recorded identities: '+','.join(missing[:20]))
          print(json.dumps({'source_identities':len(source_ids),'current_identities':len(current_ids),'missing':0}))
      volumeMounts: [{name: work, mountPath: /work, readOnly: true}]
      resources:
        requests: {cpu: 25m, memory: 64Mi}
        limits: {cpu: 200m, memory: 256Mi}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
EOF
  if ! kubectl wait "pod/$pod" -n storage --for=jsonpath='{.status.phase}'=Succeeded --timeout=10m >/dev/null; then
    kubectl logs "pod/$pod" -n storage --all-containers --tail=100 >&2 || true
    fail "SeaweedFS recovered topology does not match the recorded topology artifact"
  fi
  kubectl logs "pod/$pod" -n storage -c compare
  kubectl delete pod "$pod" -n storage --wait=true >/dev/null
}

restore_seaweedfs() {
  local artifact uri
  artifact=$(artifact_json seaweedfs); uri=$(jq -r '.artifact_locator' <<<"$artifact")
  head_s3_object seaweedfs storage seaweedfs-backup-credentials "$uri"
  kubectl wait pod -n storage -l app.kubernetes.io/name=seaweedfs --for=condition=Ready --timeout=10m >/dev/null
  verify_seaweedfs_topology "$uri"
}

restore_vault() {
  local artifact state uri auth_secret job init_plain root_token pod status vault_job_complete=false
  artifact=$(artifact_json vault); state=$(jq -r '.state' <<<"$artifact")
  if [[ "$state" == velero-fallback ]]; then
    kubectl get statefulset vault -n vault >/dev/null
    for pod in $(kubectl get pods -n vault -l app.kubernetes.io/name=vault -o name); do
      status=$(kubectl exec -n vault "${pod#pod/}" -- vault status -format=json 2>/dev/null || true)
      jq -e '.initialized == true and .sealed == false' <<<"$status" >/dev/null \
        || fail "Velero-fallback Vault member ${pod#pod/} is not initialized and unsealed"
    done
    return 0
  fi
  [[ -f "$VAULT_INIT_FILE" ]] || fail "Vault native restore requires --vault-init-file"
  [[ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" && -f "$ANSIBLE_VAULT_PASSWORD_FILE" ]] \
    || fail "ANSIBLE_VAULT_PASSWORD_FILE is required for Vault restore"
  uri=$(jq -r '.artifact_locator' <<<"$artifact"); parse_s3_uri "$uri" || fail "invalid Vault snapshot URI"
  head_s3_object vault vault vault-backup-credentials "$uri"
  init_plain="$WORK_DIR/vault-init.json"
  ansible-vault view --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE" "$VAULT_INIT_FILE" > "$init_plain"
  chmod 600 "$init_plain"
  root_token=$(jq -er '.root_token' "$init_plain")
  auth_secret="native-vault-auth-$(short_hash "$BACKUP_ID")"
  TEMP_VAULT_SECRET="$auth_secret"
  jq -n --arg token "$root_token" --arg name "$auth_secret" \
    '{apiVersion:"v1",kind:"Secret",metadata:{name:$name,namespace:"vault"},stringData:{VAULT_TOKEN:$token}}' \
    | kubectl apply -f - >/dev/null
  job="native-vault-restore-$(short_hash "$BACKUP_ID")"
  if kubectl get job "$job" -n vault -o json \
    | jq -e --arg id "$BACKUP_ID" '.metadata.annotations["backup-restore.io/native-backup-id"] == $id and
        any(.status.conditions[]?; .type == "Complete" and .status == "True")' >/dev/null 2>&1; then
    vault_job_complete=true
  fi
  if [[ "$vault_job_complete" != true ]]; then
    kubectl get job "$job" -n vault >/dev/null 2>&1 \
      && fail "existing Vault restore Job is not a completed checkpoint for this backup"
    kubectl apply -n vault -f - >/dev/null <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  labels: {backup-restore.io/native-restore: "true"}
  annotations: {backup-restore.io/native-backup-id: "${BACKUP_ID}"}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 100, runAsGroup: 1000, fsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      volumes: [{name: work, emptyDir: {sizeLimit: 2Gi}}]
      initContainers:
        - name: download
          image: amazon/aws-cli:2.34.48
          command: [/bin/sh, -ec]
          args: ['aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 cp "s3://${S3_BUCKET}/${S3_KEY}" /work/snapshot.snap; test -s /work/snapshot.snap']
          envFrom: [{secretRef: {name: vault-backup-credentials}}]
          env: [{name: HOME, value: /tmp}]
          volumeMounts: [{name: work, mountPath: /work}]
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {cpu: 500m, memory: 512Mi}
          securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
      containers:
        - name: restore
          image: hashicorp/vault:2.0.3
          command: [/bin/sh, -ec]
          args: ['vault operator raft snapshot restore -force /work/snapshot.snap']
          envFrom:
            - secretRef: {name: vault-backup-credentials}
            - secretRef: {name: ${auth_secret}}
          volumeMounts: [{name: work, mountPath: /work}]
          resources:
            requests: {cpu: 50m, memory: 128Mi}
            limits: {cpu: 500m, memory: 512Mi}
          securityContext: {runAsNonRoot: true, runAsUser: 100, runAsGroup: 1000, allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
EOF
    if ! kubectl wait "job/$job" -n vault --for=condition=complete --timeout="${TIMEOUT_SECONDS}s" >/dev/null; then
      kubectl logs "job/$job" -n vault --all-containers --tail=100 >&2 || true
      fail "Vault snapshot restore failed"
    fi
  fi
  kubectl delete secret "$auth_secret" -n vault --wait=true >/dev/null
  TEMP_VAULT_SECRET=""
  root_token=""
  for pod in $(kubectl get pods -n vault -l app.kubernetes.io/name=vault -o name); do
    status=$(kubectl exec -n vault "${pod#pod/}" -- vault status -format=json 2>/dev/null || true)
    jq -e '.initialized == true and .sealed == false' <<<"$status" >/dev/null \
      || fail "Vault member ${pod#pod/} is not initialized and unsealed after restore"
  done
}

restore_postgresql() {
  local artifact backup_cr set repo cluster cr_file secrets_file pvcs_file primary pg_phase
  artifact=$(artifact_json postgresql)
  backup_cr=$(jq -r '.name' <<<"$artifact"); set=$(jq -r '.artifact_locator' <<<"$artifact")
  repo=$(jq -r '.repository' <<<"$artifact"); cluster="${PROJECT}-pg"
  [[ "$set" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{1,127}$ && "$set" != latest && "$repo" == repo2 ]] \
    || fail "PostgreSQL requires an exact repo2 set"
  kubectl get perconapgbackup "$backup_cr" -n databases -o json \
    | jq -e --arg cluster "$cluster" --arg set "$set" --arg repo "$repo" '
      .spec.pgCluster == $cluster and .spec.repoName == $repo and .status.backupName == $set and
      ((.status.state // "" | ascii_downcase) as $s | $s == "succeeded" or $s == "successful" or $s == "ready")' \
    >/dev/null || fail "PostgreSQL backup CR does not own the exact recorded set"
  cr_file="${STATE_FILE}.postgresql-cluster.json"
  secrets_file="${STATE_FILE}.postgresql-secrets.json"
  pvcs_file="${STATE_FILE}.postgresql-pvcs.txt"
  if [[ ! -f "$cr_file" ]]; then
    kubectl get perconapgcluster "$cluster" -n databases -o json \
      | jq 'del(.metadata.annotations,.metadata.creationTimestamp,.metadata.finalizers,
          .metadata.generation,.metadata.managedFields,.metadata.resourceVersion,.metadata.uid,.status)' > "$cr_file"
    kubectl get secrets -n databases -l "postgres-operator.crunchydata.com/cluster=${cluster}" -o json \
      | jq '.items |= map(del(.metadata.creationTimestamp,.metadata.managedFields,.metadata.ownerReferences,
          .metadata.resourceVersion,.metadata.uid))' > "$secrets_file"
    kubectl get pvc -n databases -l "postgres-operator.crunchydata.com/cluster=${cluster}" -o name > "$pvcs_file"
    chmod 600 "$cr_file" "$secrets_file" "$pvcs_file"
  fi
  if kubectl get perconapgcluster "$cluster" -n databases -o json \
      | jq -e --arg id "$BACKUP_ID" '.metadata.annotations["backup-restore.io/native-backup-id"] == $id and
          .metadata.annotations["backup-restore.io/native-restore-phase"] == "completed" and .status.state == "ready"' >/dev/null 2>&1; then
    return 0
  fi
  pg_phase=$(kubectl get perconapgcluster "$cluster" -n databases -o json 2>/dev/null \
    | jq -r --arg id "$BACKUP_ID" 'if .metadata.annotations["backup-restore.io/native-backup-id"] == $id
        then .metadata.annotations["backup-restore.io/native-restore-phase"] // "" else "" end' || true)
  if [[ "$pg_phase" == restoring ]]; then
    wait_json_state perconapgcluster "$cluster" databases ready \
      || fail "in-progress PostgreSQL restore did not become ready; refusing to replace it again"
  else
    kubectl apply -f "$secrets_file" >/dev/null
    kubectl delete perconapgcluster "$cluster" -n databases --ignore-not-found \
      --wait=true --timeout="${TIMEOUT_SECONDS}s"
    while IFS= read -r pvc; do
      [[ -z "$pvc" ]] || kubectl delete "$pvc" -n databases --ignore-not-found --wait=true
    done < "$pvcs_file"
    jq --arg set "$set" --arg repo "$repo" --arg id "$BACKUP_ID" '
      .metadata.annotations["backup-restore.io/native-backup-id"]=$id |
      .metadata.annotations["backup-restore.io/native-restore-phase"]="restoring" |
      .spec.backups.pgbackrest.repos |= map(.schedules={}) |
      (.spec.backups.pgbackrest.repos[] | select(.name==$repo)) as $sourceRepo |
      .spec.dataSource.pgbackrest = {
        stanza:"db", options:["--set="+$set],
        configuration:.spec.backups.pgbackrest.configuration,
        global:.spec.backups.pgbackrest.global,
        repo:$sourceRepo
      }' "$cr_file" | kubectl apply -f - >/dev/null
    wait_json_state perconapgcluster "$cluster" databases ready || fail "PostgreSQL native restore did not become ready"
  fi
  primary=$(kubectl get pods -n databases \
    -l "postgres-operator.crunchydata.com/cluster=${cluster},postgres-operator.crunchydata.com/role=master" \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$primary" ]] || primary=$(kubectl get pods -n databases \
    -l "postgres-operator.crunchydata.com/cluster=${cluster},postgres-operator.crunchydata.com/data=postgres" \
    -o jsonpath='{.items[0].metadata.name}')
  kubectl exec -n databases "$primary" -- psql -U postgres -tAc 'select 1' | grep -qx 1 \
    || fail "PostgreSQL validation query failed"
  jq --arg id "$BACKUP_ID" '
    .metadata.annotations["backup-restore.io/native-backup-id"]=$id |
    .metadata.annotations["backup-restore.io/native-restore-phase"]="completed" |
    del(.spec.dataSource)' "$cr_file" | kubectl apply -f - >/dev/null
  wait_json_state perconapgcluster "$cluster" databases ready \
    || fail "PostgreSQL did not return to its steady backup schedule after restore"
}

restore_mongodb() {
  local artifact backup_cr destination storage cluster restore_name cluster_json backup_json source
  artifact=$(artifact_json mongodb); backup_cr=$(jq -r '.name' <<<"$artifact")
  destination=$(jq -r '.artifact_locator' <<<"$artifact"); storage=$(jq -r '.repository' <<<"$artifact")
  cluster="${PROJECT}-mongo"
  [[ "$destination" == *://* && "$destination" != *latest* ]] || fail "MongoDB destination is not exact"
  backup_json=$(kubectl get perconaservermongodbbackup "$backup_cr" -n databases -o json)
  jq -e --arg destination "$destination" '(.status.state | ascii_downcase) == "ready" and .status.destination == $destination' \
    <<<"$backup_json" \
    >/dev/null || fail "MongoDB backup CR does not match the exact destination"
  cluster_json=$(kubectl get perconaservermongodb "$cluster" -n databases -o json)
  source=$(jq -cn --arg destination "$destination" --arg storage "$storage" \
    --argjson cluster "$cluster_json" --argjson backup "$backup_json" \
    '{destination:$destination,type:($backup.status.type // "logical"),storageName:$storage,
      s3:($cluster.spec.backup.storages[$storage].s3)}')
  restore_name="native-mongo-$(short_hash "$BACKUP_ID")"
  jq -n --arg name "$restore_name" --arg cluster "$cluster" --argjson source "$source" '
    {apiVersion:"psmdb.percona.com/v1",kind:"PerconaServerMongoDBRestore",
     metadata:{name:$name,namespace:"databases",annotations:{"backup-restore.io/native-backup-id":""}},
     spec:{clusterName:$cluster,backupSource:$source}}' \
    | jq --arg id "$BACKUP_ID" '.metadata.annotations["backup-restore.io/native-backup-id"]=$id' \
    | kubectl apply -f - >/dev/null
  wait_json_state perconaservermongodbrestore "$restore_name" databases ready \
    || fail "MongoDB native restore failed"
  wait_json_state perconaservermongodb "$cluster" databases ready || fail "MongoDB cluster is not ready"
  kubectl exec -n databases "${cluster}-rs0-0" -c mongod -- mongosh --quiet \
    --eval 'quit(db.adminCommand({ping:1}).ok == 1 ? 0 : 2)' >/dev/null \
    || fail "MongoDB validation ping failed"
}

restore_gitlab_secrets() {
  local artifact uri pod encoded file expected_secret_sha actual_secret_sha
  artifact=$(artifact_json gitlab-secrets); uri=$(jq -r '.artifact_locator' <<<"$artifact")
  head_s3_object gitlab-secrets gitlab gitlab-rails-backup-credentials "$uri"
  parse_s3_uri "$uri" || fail "invalid GitLab Rails secret URI"
  pod="native-gitlab-secret-$(short_hash "$BACKUP_ID")"
  kubectl delete pod "$pod" -n gitlab --ignore-not-found --wait=true >/dev/null
  kubectl apply -n gitlab -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata: {name: ${pod}}
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  activeDeadlineSeconds: 600
  securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
  containers:
    - name: download
      image: amazon/aws-cli:2.34.48
      command: [/bin/sh, -ec]
      args: ['aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 cp "s3://${S3_BUCKET}/${S3_KEY}" - --only-show-errors | base64']
      envFrom: [{secretRef: {name: gitlab-rails-backup-credentials}}]
      env: [{name: HOME, value: /tmp}]
      resources:
        requests: {cpu: 25m, memory: 64Mi}
        limits: {cpu: 200m, memory: 256Mi}
      securityContext: {allowPrivilegeEscalation: false, readOnlyRootFilesystem: true, capabilities: {drop: ["ALL"]}}
EOF
  kubectl wait "pod/$pod" -n gitlab --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null \
    || fail "GitLab Rails secret download failed"
  encoded=$(kubectl logs "pod/$pod" -n gitlab)
  kubectl delete pod "$pod" -n gitlab --wait=true >/dev/null
  file="$WORK_DIR/gitlab-secrets.yml"
  printf '%s' "$encoded" | base64 --decode > "$file"
  chmod 600 "$file"; [[ -s "$file" ]] || fail "GitLab Rails secret object is empty"
  kubectl create secret generic gitlab-rails-secret -n gitlab --from-file="secrets.yml=$file" \
    --dry-run=client -o yaml | kubectl apply -f - >/dev/null
  expected_secret_sha=$(sha256_file "$file")
  actual_secret_sha=$(kubectl get secret gitlab-rails-secret -n gitlab \
    -o jsonpath='{.data.secrets\.yml}' | base64 --decode | \
    { if command -v sha256sum >/dev/null 2>&1; then sha256sum; else shasum -a 256; fi; } | awk '{print $1}')
  [[ "$actual_secret_sha" == "$expected_secret_sha" ]] \
    || fail "applied GitLab Rails secret does not match the exact backup object"
}

restore_gitlab() {
  local artifact uri backup_file backup_id job replicas_file
  artifact=$(artifact_json gitlab); uri=$(jq -r '.artifact_locator' <<<"$artifact")
  parse_s3_uri "$uri" || fail "invalid GitLab Toolbox URI"
  backup_file="$S3_KEY"; backup_id="${backup_file%_gitlab_backup.tar}"
  [[ "$backup_id" != "$backup_file" && -n "$backup_id" ]] || fail "GitLab archive key has no exact backup ID"
  replicas_file="${STATE_FILE}.gitlab-replicas.json"
  GITLAB_REPLICAS_FILE="$replicas_file"
  if [[ ! -f "$replicas_file" ]]; then
    kubectl get deployment -n gitlab -l 'app in (webservice,sidekiq)' -o json \
      | jq '[.items[] | {name:.metadata.name,replicas:(.spec.replicas // 1)}]' > "$replicas_file"
    chmod 600 "$replicas_file"
  fi
  GITLAB_SCALED=true
  while IFS=$'\t' read -r deployment _replicas; do
    [[ -z "$deployment" ]] || kubectl scale deployment "$deployment" -n gitlab --replicas=0 >/dev/null
  done < <(jq -r '.[] | [.name,.replicas] | @tsv' "$replicas_file")
  job="native-gitlab-restore-$(short_hash "$BACKUP_ID")"
  if ! kubectl get job "$job" -n gitlab -o json \
      | jq -e --arg id "$BACKUP_ID" '.metadata.annotations["backup-restore.io/native-backup-id"] == $id and
          any(.status.conditions[]?; .type == "Complete" and .status == "True")' >/dev/null 2>&1; then
    kubectl get job "$job" -n gitlab >/dev/null 2>&1 \
      && fail "existing GitLab restore Job is not a completed checkpoint for this backup"
    kubectl get cronjob gitlab-toolbox-backup -n gitlab -o json \
      | jq --arg name "$job" --arg id "$BACKUP_ID" --arg restoreId "$backup_id" '
          {apiVersion:"batch/v1",kind:"Job",
           metadata:{name:$name,namespace:"gitlab",labels:{"backup-restore.io/native-restore":"true"},
             annotations:{"backup-restore.io/native-backup-id":$id}},
           spec:.spec.jobTemplate.spec} |
          .spec.backoffLimit=0 | del(.spec.ttlSecondsAfterFinished) |
          .spec.template.spec.restartPolicy="Never" |
          .spec.template.spec.containers[0].command=["backup-utility"] |
          .spec.template.spec.containers[0].args=["--restore","-t",$restoreId,"--skip","db",
            "--s3tool","awscli","--aws-s3-endpoint-url","http://seaweedfs-filer.storage.svc.cluster.local:8333"]' \
      | kubectl apply -f - >/dev/null
    if ! kubectl wait "job/$job" -n gitlab --for=condition=complete --timeout="${TIMEOUT_SECONDS}s" >/dev/null; then
      kubectl logs "job/$job" -n gitlab --all-containers --tail=100 >&2 || true
      fail "GitLab Toolbox restore failed"
    fi
  fi
  while IFS=$'\t' read -r deployment replicas; do
    [[ -z "$deployment" ]] || kubectl scale deployment "$deployment" -n gitlab --replicas="$replicas" >/dev/null
  done < <(jq -r '.[] | [.name,.replicas] | @tsv' "$replicas_file")
  kubectl rollout status deployment -n gitlab --timeout=20m >/dev/null
  GITLAB_SCALED=false
}

for component in seaweedfs vault postgresql mongodb gitlab-secrets gitlab; do
  if component_done "$component"; then
    log "$component already completed; preserving checkpoint"
    continue
  fi
  if ! component_enabled "$component"; then
    checkpoint "$component" completed disabled
    continue
  fi
  checkpoint "$component" running "mutation or verification started"
  log "restoring $component"
  case "$component" in
    seaweedfs) restore_seaweedfs ;;
    vault) restore_vault ;;
    postgresql) restore_postgresql ;;
    mongodb) restore_mongodb ;;
    gitlab-secrets) restore_gitlab_secrets ;;
    gitlab) restore_gitlab ;;
  esac
  checkpoint "$component" completed verified
done

"${SCRIPT_DIR}/health-gates.sh" --config "$CONFIG"
# jq variables are expanded by jq, not by the shell.
# shellcheck disable=SC2016
write_state --arg now "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  '.status="completed" | .completed_at=$now | .updated_at=$now'
log "native replay and platform health gates completed"
