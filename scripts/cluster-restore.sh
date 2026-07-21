#!/usr/bin/env bash
# Verify and restore encrypted full-cluster recovery bundles.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
ARCHIVE=""
MODE=verify
IDENTITY="${CLUSTER_BACKUP_AGE_IDENTITY:-}"
OUTPUT_DIR=""
VELERO_BACKUP=""
RECOVERY_INVENTORY_INPUT=""
SURVIVOR=""
BROKEN_NODES=()
CONFIRMATION=""
DRY_RUN=false
FORCE=false
RESTORE_TIMEOUT="${CLUSTER_RESTORE_TIMEOUT_SECONDS:-28800}"
ALLOW_BACKUP_WARNINGS=0
ALLOW_RESTORE_WARNINGS=0
STAGED_OUTPUT=""
RECEIPT_SCHEMA=0

log() { printf '[cluster-restore] %s\n' "$*"; }
fail() { printf '[cluster-restore] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[cluster-restore] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: cluster-restore.sh --archive FILE [OPTIONS]

Modes:
  verify   Decrypt, validate both checksum layers, and inspect the manifest.
  operator-state  Securely materialize exact replacement-cluster operator state.
  velero   Restore Kubernetes resources and PVC data into a replacement cluster.
  etcd     Recover a lost-quorum Kubespray control plane from the etcd snapshot.

Options:
  --archive FILE          Encrypted .age or .enc cluster bundle
  --mode MODE             verify|velero|etcd (default: verify)
  --identity FILE         age identity file
  --output-dir DIR        New destination directory for operator-state mode
  --velero-backup NAME    Override the backup name recorded in the bundle
  --allow-backup-warnings N  Explicitly reviewed Velero Backup warning count
  --allow-restore-warnings N Explicitly reviewed Velero Restore warning count
  --inventory FILE        Updated Kubespray inventory for replacement nodes
  --survivor NODE         Surviving first control-plane/etcd node for etcd mode
  --broken-node NODE      Broken control-plane/etcd node; repeat as needed
  --confirm TEXT          Exact destructive confirmation phrase
  --dry-run               Verify and show destructive actions without applying
  --force                 Skip the interactive confirmation prompt
  -h, --help              Show this help

For OpenSSL bundles, set CLUSTER_BACKUP_PASSPHRASE. The etcd mode deliberately
refuses to run while the Kubernetes API is healthy and uses Kubespray's pinned
recover-control-plane playbook. A total control-plane loss is restored by
building a replacement cluster and using velero mode.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --archive) ARCHIVE="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --identity) IDENTITY="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --velero-backup) VELERO_BACKUP="$2"; shift 2 ;;
    --allow-backup-warnings) ALLOW_BACKUP_WARNINGS="$2"; shift 2 ;;
    --allow-restore-warnings) ALLOW_RESTORE_WARNINGS="$2"; shift 2 ;;
    --inventory) RECOVERY_INVENTORY_INPUT="$2"; shift 2 ;;
    --survivor) SURVIVOR="$2"; shift 2 ;;
    --broken-node) BROKEN_NODES+=("$2"); shift 2 ;;
    --confirm) CONFIRMATION="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ "$MODE" =~ ^(verify|operator-state|velero|etcd)$ ]] || fail "invalid mode: $MODE"
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || fail "--archive must name an existing file"
[[ "$ALLOW_BACKUP_WARNINGS" =~ ^[0-9]+$ ]] || fail "--allow-backup-warnings must be a non-negative integer"
[[ "$ALLOW_RESTORE_WARNINGS" =~ ^[0-9]+$ ]] || fail "--allow-restore-warnings must be a non-negative integer"
[[ "$MODE" != operator-state || -n "$OUTPUT_DIR" ]] || fail "operator-state mode requires --output-dir"
for tool in tar jq python3; do command -v "$tool" >/dev/null || fail "required tool is missing: $tool"; done

umask 077
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cluster-restore.XXXXXX")
PLAIN_ARCHIVE="${WORK_DIR}/bundle.tar.gz"
cleanup() {
  [[ -z "$STAGED_OUTPUT" ]] || rm -rf "$STAGED_OUTPUT"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

verify_external_checksum() {
  local checksum_file="${ARCHIVE}.sha256" expected actual
  [[ -f "$checksum_file" ]] || fail "archive checksum sidecar is missing: $checksum_file"
  expected=$(awk '{print $1}' "$checksum_file")
  if command -v sha256sum >/dev/null; then actual=$(sha256sum "$ARCHIVE" | awk '{print $1}'); else actual=$(shasum -a 256 "$ARCHIVE" | awk '{print $1}'); fi
  [[ "$expected" == "$actual" ]] || fail "encrypted archive checksum mismatch"
  ARCHIVE_SHA256="$actual"
}

decrypt_archive() {
  case "$ARCHIVE" in
    *.age)
      command -v age >/dev/null || fail "age is required to decrypt this archive"
      [[ -n "$IDENTITY" && -f "$IDENTITY" ]] || fail "--identity is required for an age archive"
      age -d -i "$IDENTITY" -o "$PLAIN_ARCHIVE" "$ARCHIVE"
      ;;
    *.enc)
      command -v openssl >/dev/null || fail "openssl is required to decrypt this archive"
      [[ -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] || fail "CLUSTER_BACKUP_PASSPHRASE is required"
      openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
        -pass env:CLUSTER_BACKUP_PASSPHRASE -in "$ARCHIVE" -out "$PLAIN_ARCHIVE"
      ;;
    *) fail "archive extension must be .age or .enc" ;;
  esac
}

verify_external_checksum
decrypt_archive
# Validate every member before extracting anything. Links and special files are
# unnecessary in a recovery bundle and can redirect later copies outside the
# private work directory even when their own member name has no '..'.
python3 - "$PLAIN_ARCHIVE" "$WORK_DIR" <<'PY' \
  || fail "archive contains an unsafe path or member type"
import pathlib
import sys
import tarfile

archive, destination = sys.argv[1:]
with tarfile.open(archive, "r:gz") as source:
    members = source.getmembers()
    for member in members:
        path = pathlib.PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not (member.isdir() or member.isreg()):
            raise SystemExit(1)
    source.extractall(destination, members=members)
PY
BUNDLE_DIR=$(find "$WORK_DIR" -mindepth 1 -maxdepth 1 -type d -name '*-cluster-*' -print | head -1)
[[ -n "$BUNDLE_DIR" && -f "$BUNDLE_DIR/MANIFEST.json" ]] || fail "bundle manifest is missing"
(
  cd "$BUNDLE_DIR"
  if command -v sha256sum >/dev/null; then sha256sum -c SHA256SUMS; else shasum -a 256 -c SHA256SUMS; fi
) >/dev/null
jq -e '
  (.schema_version == 1 or .schema_version == 2) and
  .backup_id and .project and .source_context and
  (if .schema_version == 2 then
     ((.recovery_dependencies.vault_init.required // false) == false or
       .recovery_dependencies.vault_init.included == true) and
     (.contains.vault_init_material ==
       (.recovery_dependencies.vault_init.included // false)) and
     (if (.recovery_dependencies.vault_init.included // false) then
        .recovery_dependencies.vault_init.encryption == "ansible-vault" and
        .recovery_dependencies.vault_init.bundle_path == "config/vault-init.json.vault"
      else true end)
   else true end)
' "$BUNDLE_DIR/MANIFEST.json" >/dev/null \
  || fail "bundle manifest recovery dependency contract is invalid"
VAULT_INIT_INCLUDED=$(jq -r '.recovery_dependencies.vault_init.included // false' "$BUNDLE_DIR/MANIFEST.json")
if [[ "$VAULT_INIT_INCLUDED" == true ]]; then
  BUNDLED_VAULT_INIT="$BUNDLE_DIR/config/vault-init.json.vault"
  [[ -f "$BUNDLED_VAULT_INIT" && -s "$BUNDLED_VAULT_INIT" ]] \
    || fail "bundle is missing required encrypted Vault initialization material"
  IFS= read -r vault_init_header < "$BUNDLED_VAULT_INIT" || true
  [[ "$vault_init_header" == "\$ANSIBLE_VAULT;"* ]] \
    || fail "bundled Vault initialization material is not Ansible Vault encrypted"
fi
BUNDLE_SCHEMA=$(jq -r '.schema_version' "$BUNDLE_DIR/MANIFEST.json")
BACKUP_ID=$(jq -r '.backup_id' "$BUNDLE_DIR/MANIFEST.json")
if [[ "$BUNDLE_SCHEMA" == 2 ]]; then
  # Schema-v2 bundles are created by the atomic remote-publication workflow.
  # Archive and checksum objects are uploaded before the verified completion
  # receipt, so accepting those two objects alone would treat an interrupted
  # publication as a usable recovery point.
  RECEIPT_PATH="${ARCHIVE}.manifest.json"
  [[ -f "$RECEIPT_PATH" && -s "$RECEIPT_PATH" ]] \
    || fail "schema-v2 bundle completion receipt is missing: $RECEIPT_PATH"
  jq -e --arg backupId "$BACKUP_ID" --arg archive "$(basename "$ARCHIVE")" \
    --arg sha256 "$ARCHIVE_SHA256" '
      (.schema_version == 1 or .schema_version == 2) and
      .receipt_type == "encrypted-cluster-backup" and
      .backup_id == $backupId and
      .archive == $archive and
      .sha256 == $sha256 and
      .completeness == "complete" and
      .remote.published == true and
      .remote.download_sha256_verified == true and
      .remote.receipt_uploaded_last == true and
      .remote.publication_state == "complete" and
      (.remote.endpoint | type == "string" and length > 0) and
      (.remote.bucket | type == "string" and length > 0) and
      (.remote.archive_key | type == "string" and length > 0) and
      (.remote.checksum_key | type == "string" and length > 0) and
      (.remote.receipt_key | type == "string" and length > 0)
    ' "$RECEIPT_PATH" >/dev/null \
    || fail "schema-v2 bundle completion receipt is incomplete or does not match the archive"
  RECEIPT_SCHEMA=$(jq -r '.schema_version' "$RECEIPT_PATH")
else
  log "legacy schema-v1 bundle: no remote completion receipt contract is available"
fi
PROJECT=$(jq -r '.project' "$BUNDLE_DIR/MANIFEST.json")
[[ "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || fail "bundle project name is unsafe"
SOURCE_CONTEXT=$(jq -r '.source_context' "$BUNDLE_DIR/MANIFEST.json")
SOURCE_CLUSTER_UID=$(jq -r '.source_cluster_uid // ""' "$BUNDLE_DIR/MANIFEST.json")
COMPLETENESS=$(jq -r '.completeness' "$BUNDLE_DIR/MANIFEST.json")
[[ "$COMPLETENESS" == complete ]] || fail "bundle is explicitly marked incomplete"
if [[ "$BUNDLE_SCHEMA" == 2 && "$RECEIPT_SCHEMA" == 2 ]]; then
  jq -e --arg project "$PROJECT" --arg sourceUid "$SOURCE_CLUSTER_UID" \
    --arg prefix "$(jq -r '.velero_storage_prefix // ""' "$BUNDLE_DIR/MANIFEST.json")" \
    --arg velero "$(jq -r '.velero_backup_name // ""' "$BUNDLE_DIR/MANIFEST.json")" '
      .project == $project and .source_cluster_uid == $sourceUid and
      .velero_storage_prefix == $prefix and .velero_backup_name == $velero
    ' "$RECEIPT_PATH" >/dev/null \
    || fail "schema-v2 receipt identity does not match the encrypted bundle"
fi
log "verified bundle $(jq -r '.backup_id' "$BUNDLE_DIR/MANIFEST.json") for project $PROJECT"

if [[ "$MODE" == verify ]]; then
  jq '{backup_id,created_at,project,domain,profile,source_context,source_cluster_uid,completeness,
    velero_backup_name,velero_storage_prefix,native_backup_catalog,contains,
    recovery_dependencies,restore_order}' \
    "$BUNDLE_DIR/MANIFEST.json"
  exit 0
fi

if [[ "$MODE" == operator-state ]]; then
  command -v install >/dev/null || fail "install is required for secure operator-state materialization"
  [[ ! -e "$OUTPUT_DIR" ]] \
    || fail "operator-state destination already exists; choose a new directory: $OUTPUT_DIR"
  OUTPUT_PARENT=$(dirname "$OUTPUT_DIR")
  OUTPUT_NAME=$(basename "$OUTPUT_DIR")
  [[ -n "$OUTPUT_NAME" && "$OUTPUT_NAME" != . && "$OUTPUT_NAME" != .. ]] \
    || fail "invalid operator-state destination"
  mkdir -p "$OUTPUT_PARENT"
  OUTPUT_PARENT=$(cd "$OUTPUT_PARENT" && pwd)
  STAGED_OUTPUT=$(mktemp -d "${OUTPUT_PARENT}/.${OUTPUT_NAME}.staged.XXXXXX")
  chmod 0700 "$STAGED_OUTPUT"
  install -m 0600 "$BUNDLE_DIR/config/platform.yaml" "$STAGED_OUTPUT/platform.yaml"
  install -m 0600 "$BUNDLE_DIR/config/platform-secrets.yml" "$STAGED_OUTPUT/.platform-secrets.yml"
  if [[ "$VAULT_INIT_INCLUDED" == true ]]; then
    install -m 0600 "$BUNDLED_VAULT_INIT" "$STAGED_OUTPUT/.vault-init-${PROJECT}.json"
  fi
  install -d -m 0700 "$STAGED_OUTPUT/repository"
  for repository_file in repository.bundle worktree.patch repository-untracked.tar \
    repository-untracked-files.txt repository-untracked-count.txt git-status.txt; do
    [[ -f "$BUNDLE_DIR/config/$repository_file" ]] \
      || fail "bundle is missing repository recovery input: $repository_file"
    install -m 0600 "$BUNDLE_DIR/config/$repository_file" "$STAGED_OUTPUT/repository/$repository_file"
  done
  install -m 0600 "$BUNDLE_DIR/MANIFEST.json" "$STAGED_OUTPUT/MANIFEST.json"
  if [[ -f "$BUNDLE_DIR/application-backups/native-backups.json" ]]; then
    install -m 0600 "$BUNDLE_DIR/application-backups/native-backups.json" \
      "$STAGED_OUTPUT/native-backups.json"
  fi
  jq '{schema_version:1,backup_id,created_at,project,domain,profile,
      source_context,source_cluster_uid,velero_backup_name,velero_storage_prefix,
      native_backup_catalog,recovery_dependencies,restore_order}' \
    "$BUNDLE_DIR/MANIFEST.json" > "$STAGED_OUTPUT/recovery-state.json"
  chmod 0600 "$STAGED_OUTPUT/recovery-state.json"
  mv "$STAGED_OUTPUT" "${OUTPUT_PARENT}/${OUTPUT_NAME}"
  STAGED_OUTPUT=""
  log "materialized exact operator state at ${OUTPUT_PARENT}/${OUTPUT_NAME}"
  log "use platform.yaml with the recovered .platform-secrets.yml and .vault-init-${PROJECT}.json; no secret material was printed"
  exit 0
fi

if [[ "$MODE" == velero ]]; then
  command -v kubectl >/dev/null || fail "kubectl is required for Velero restore"
  [[ "$BUNDLE_SCHEMA" == 2 && "$RECEIPT_SCHEMA" == 2 ]] \
    || fail "strict replacement restore requires a schema-v2 bundle and fresh schema-v2 completion receipt"
  PVC_EVIDENCE="$BUNDLE_DIR/application-backups/pvc-protection-evidence.json"
  PVB_EVIDENCE="$BUNDLE_DIR/application-backups/pod-volume-backups.json"
  EXPECTED_MOUNTS="$BUNDLE_DIR/application-backups/mounted-pod-volumes.expected.tsv"
  COMPLETED_MOUNTS="$BUNDLE_DIR/application-backups/mounted-pod-volumes.completed.tsv"
  NATIVE_CATALOG="$BUNDLE_DIR/application-backups/native-backups.json"
  for required_evidence in "$PVC_EVIDENCE" "$PVB_EVIDENCE" "$EXPECTED_MOUNTS" \
    "$COMPLETED_MOUNTS" "$NATIVE_CATALOG"; do
    [[ -f "$required_evidence" ]] || fail "bundle restore evidence is missing: $required_evidence"
  done
  jq -e --arg project "$PROJECT" '
    .velero_backup == "completed" and
    .pvc_protection_gate.status == "complete" and
    .pvc_protection_gate.failures == 0 and
    .native_backup_catalog.included == true
  ' "$BUNDLE_DIR/MANIFEST.json" >/dev/null \
    || fail "bundle did not pass its Velero, PVC, and native-backup completeness gates"
  jq -e --arg project "$PROJECT" '
    .schema_version == 1 and .project == $project and .completeness == "complete" and
    .summary.failed == 0 and .summary.expected == (.artifacts | length) and
    all(.artifacts[]; .state == "completed" or .state == "velero-fallback" or .state == "disabled")
  ' "$NATIVE_CATALOG" >/dev/null || fail "native backup catalog is incomplete or belongs to another project"
  jq -e '.status == "complete" and .summary.failures == 0 and all(.claims[]; .protected == true)' \
    "$PVC_EVIDENCE" >/dev/null || fail "bundled PVC protection evidence is incomplete"
  jq -e '[.items[] | select((.status.phase // "") != "Completed")] | length == 0' \
    "$PVB_EVIDENCE" >/dev/null || fail "bundled PodVolumeBackup evidence contains an incomplete item"
  comm -23 "$EXPECTED_MOUNTS" "$COMPLETED_MOUNTS" > "$WORK_DIR/missing-source-volume-backups.tsv"
  [[ ! -s "$WORK_DIR/missing-source-volume-backups.tsv" ]] \
    || fail "bundle lacks a completed PodVolumeBackup for one or more expected mounted volumes"
  EXPECTED_PVR_COUNT=$(jq '.items | length' "$PVB_EVIDENCE")
  kubectl cluster-info >/dev/null
  TARGET_CONTEXT=$(kubectl config current-context)
  TARGET_CLUSTER_UID=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}')
  [[ -n "$TARGET_CLUSTER_UID" ]] || fail "could not determine the target cluster UID"
  if [[ -n "$SOURCE_CLUSTER_UID" ]]; then
    [[ "$TARGET_CLUSTER_UID" != "$SOURCE_CLUSTER_UID" ]] \
      || fail "Velero restore must target a replacement cluster, not source cluster UID $SOURCE_CLUSTER_UID"
  else
    # Schema-v1 and early schema-v2 bundles predate cluster UID capture. Keep
    # their context-name guard as a conservative compatibility boundary.
    [[ "$TARGET_CONTEXT" != "$SOURCE_CONTEXT" ]] \
      || fail "legacy bundle has no source cluster UID; target context must differ from source context $SOURCE_CONTEXT"
  fi
  RECORDED_VELERO_BACKUP=$(jq -r '.velero_backup_name // ""' "$BUNDLE_DIR/MANIFEST.json")
  if [[ -n "$VELERO_BACKUP" && "$VELERO_BACKUP" != "$RECORDED_VELERO_BACKUP" ]]; then
    fail "--velero-backup must exactly match the backup recorded in a schema-v2 bundle"
  fi
  VELERO_BACKUP="$RECORDED_VELERO_BACKUP"
  [[ -n "$VELERO_BACKUP" && "$VELERO_BACKUP" != null ]] || fail "bundle has no Velero backup name"
  kubectl wait backupstoragelocation/default -n velero --for=jsonpath='{.status.phase}'=Available --timeout=300s
  TARGET_BACKUP_JSON="$WORK_DIR/target-backup.json"
  kubectl get backup "$VELERO_BACKUP" -n velero -o json > "$TARGET_BACKUP_JSON" \
    || fail "Velero backup is not synchronized into the target cluster: $VELERO_BACKUP"
  jq -e --argjson allowedWarnings "$ALLOW_BACKUP_WARNINGS" '
    .status.phase == "Completed" and (.status.errors // 0) == 0 and
    (.status.warnings // 0) <= $allowedWarnings
  ' "$TARGET_BACKUP_JSON" >/dev/null \
    || fail "Velero source Backup is not Completed with zero errors and an explicitly allowed warning count"
  RESTORE_NAME="${VELERO_BACKUP}-restore-$(date -u +%Y%m%d%H%M%S)"
  phrase="RESTORE_${PROJECT}"
  if [[ "$DRY_RUN" == true ]]; then
    dry "would create Velero Restore $RESTORE_NAME in target context $TARGET_CONTEXT"
    dry "would restore all Kubernetes resources and filesystem-backed PVC data from $VELERO_BACKUP"
    exit 0
  fi
  if [[ "$CONFIRMATION" != "$phrase" ]]; then
    [[ "$FORCE" == true ]] && fail "--force still requires --confirm $phrase"
    printf 'Type %s to restore into %s: ' "$phrase" "$TARGET_CONTEXT"
    read -r CONFIRMATION
  fi
  [[ "$CONFIRMATION" == "$phrase" ]] || fail "confirmation did not match $phrase"
  kubectl apply -f - <<EOF
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: ${RESTORE_NAME}
  namespace: velero
spec:
  backupName: ${VELERO_BACKUP}
  restorePVs: true
  existingResourcePolicy: none
  preserveNodePorts: true
EOF
  deadline=$((SECONDS + RESTORE_TIMEOUT))
  while (( SECONDS < deadline )); do
    phase=$(kubectl get restore "$RESTORE_NAME" -n velero -o jsonpath='{.status.phase}' 2>/dev/null || echo New)
    case "$phase" in
      Completed) break ;;
      Failed|PartiallyFailed|FailedValidation) kubectl get restore "$RESTORE_NAME" -n velero -o yaml; fail "Velero restore ended in phase $phase" ;;
    esac
    sleep 15
  done
  [[ "${phase:-}" == Completed ]] || fail "Velero restore timed out"
  EVIDENCE_PREFIX="${ARCHIVE}.${RESTORE_NAME}"
  install -m 0600 "$TARGET_BACKUP_JSON" "${EVIDENCE_PREFIX}.backup.json"
  kubectl get restore "$RESTORE_NAME" -n velero -o json > "${EVIDENCE_PREFIX}.restore.json"
  jq -e --argjson allowedWarnings "$ALLOW_RESTORE_WARNINGS" '
    .status.phase == "Completed" and (.status.errors // 0) == 0 and
    (.status.warnings // 0) <= $allowedWarnings
  ' "${EVIDENCE_PREFIX}.restore.json" >/dev/null \
    || fail "Velero Restore completed with errors or unreviewed warnings"

  # Restore phase completion can precede the final status update for individual
  # filesystem transfers. Require one completed PVR for every source PVB.
  pvr_deadline=$((SECONDS + RESTORE_TIMEOUT))
  while (( SECONDS < pvr_deadline )); do
    kubectl get podvolumerestores -n velero \
      -l "velero.io/restore-name=${RESTORE_NAME}" -o json \
      > "${EVIDENCE_PREFIX}.pod-volume-restores.json"
    failed_pvrs=$(jq '[.items[] | select((.status.phase // "") == "Failed" or (.status.phase // "") == "PartiallyFailed")] | length' \
      "${EVIDENCE_PREFIX}.pod-volume-restores.json")
    (( failed_pvrs == 0 )) || fail "$failed_pvrs Velero PodVolumeRestore item(s) failed"
    completed_pvrs=$(jq '[.items[] | select((.status.phase // "") == "Completed")] | length' \
      "${EVIDENCE_PREFIX}.pod-volume-restores.json")
    total_pvrs=$(jq '.items | length' "${EVIDENCE_PREFIX}.pod-volume-restores.json")
    if (( completed_pvrs == total_pvrs && completed_pvrs == EXPECTED_PVR_COUNT )); then break; fi
    sleep 15
  done
  (( ${completed_pvrs:-0} == ${total_pvrs:-0} && ${completed_pvrs:-0} == EXPECTED_PVR_COUNT )) \
    || fail "Velero PodVolumeRestore coverage timed out (${completed_pvrs:-0}/${EXPECTED_PVR_COUNT} completed)"

  # The source evidence names every protected claim. Wait until each exact
  # claim is Bound and mounted by a Running pod whose container mounts that
  # volume; mere PVC object recreation is not recovery proof.
  pvc_deadline=$((SECONDS + RESTORE_TIMEOUT))
  while (( SECONDS < pvc_deadline )); do
    kubectl get persistentvolumeclaims --all-namespaces -o json > "$WORK_DIR/target-pvcs.json"
    kubectl get pods --all-namespaces -o json > "$WORK_DIR/target-pods.json"
    jq -n --slurpfile expected "$PVC_EVIDENCE" --slurpfile pvc "$WORK_DIR/target-pvcs.json" \
      --slurpfile pods "$WORK_DIR/target-pods.json" '
      [
        $expected[0].claims[] as $claim
        | ($pvc[0].items | map(select(.metadata.namespace == $claim.namespace and .metadata.name == $claim.name)) | first // {}) as $target
        | ([
            $pods[0].items[]
            | select(.metadata.namespace == $claim.namespace and (.status.phase // "") == "Running")
            | ([.spec.initContainers[]?,.spec.containers[]?,.spec.ephemeralContainers[]?]
               | [.[].volumeMounts[]?.name] | unique) as $mounted
            | .spec.volumes[]? as $volume
            | select($volume.persistentVolumeClaim.claimName == $claim.name and
                     ($mounted | index($volume.name)))
            | $volume
          ] | length) as $mounts
        | select(($target.status.phase // "") != "Bound" or $mounts == 0)
        | {namespace:$claim.namespace,name:$claim.name,
           phase:($target.status.phase // "Missing"),running_mounts:$mounts}
      ]
    ' > "${EVIDENCE_PREFIX}.missing-pvc-coverage.json"
    missing_claims=$(jq 'length' "${EVIDENCE_PREFIX}.missing-pvc-coverage.json")
    (( missing_claims > 0 )) || break
    sleep 15
  done
  (( ${missing_claims:-1} == 0 )) \
    || fail "$missing_claims restored PVC(s) are not Bound and mounted by Running pods"
  install -m 0600 "$WORK_DIR/target-pvcs.json" "${EVIDENCE_PREFIX}.persistentvolumeclaims.json"
  install -m 0600 "$WORK_DIR/target-pods.json" "${EVIDENCE_PREFIX}.pods.json"

  [[ -x "$SCRIPT_DIR/health-gates.sh" ]] || fail "health-gates.sh is missing or not executable"
  health_deadline=$((SECONDS + RESTORE_TIMEOUT))
  until "$SCRIPT_DIR/health-gates.sh" --config "$BUNDLE_DIR/config/platform.yaml" \
    > "${EVIDENCE_PREFIX}.health.log" 2>&1; do
    (( SECONDS < health_deadline )) || fail "replacement-cluster health gates timed out"
    sleep 30
  done
  log "Velero restore completed: $RESTORE_NAME"
  exit 0
fi

command -v kubectl >/dev/null || fail "kubectl is required for etcd safety checks"
if kubectl get --raw='/readyz' >/dev/null 2>&1; then
  fail "the Kubernetes API is healthy; etcd disaster recovery is intentionally refused"
fi
[[ -n "$SURVIVOR" ]] || fail "etcd mode requires --survivor"
(( ${#BROKEN_NODES[@]} > 0 )) || fail "etcd mode requires at least one --broken-node"
for node in "${BROKEN_NODES[@]}"; do [[ "$node" != "$SURVIVOR" ]] || fail "survivor cannot also be broken"; done
[[ -s "$BUNDLE_DIR/etcd/snapshot.db" ]] || fail "bundle has no etcd snapshot"

RECOVERY_INVENTORY="$WORK_DIR/recovery-hosts.yml"
if [[ -n "$RECOVERY_INVENTORY_INPUT" ]]; then
  [[ -f "$RECOVERY_INVENTORY_INPUT" ]] || fail "recovery inventory does not exist: $RECOVERY_INVENTORY_INPUT"
  cp "$RECOVERY_INVENTORY_INPUT" "$RECOVERY_INVENTORY"
else
  cp "$BUNDLE_DIR/config/kubespray-hosts.yml" "$RECOVERY_INVENTORY"
fi
for node in "$SURVIVOR" "${BROKEN_NODES[@]}"; do
  NODE="$node" yq -e '.all.hosts[strenv(NODE)] != null' "$RECOVERY_INVENTORY" >/dev/null \
    || fail "node ${node} is absent from the recovery inventory"
  NODE="$node" yq -e '.all.children.etcd.hosts[strenv(NODE)] != null and .all.children.kube_control_plane.hosts[strenv(NODE)] != null' \
    "$RECOVERY_INVENTORY" >/dev/null \
    || fail "node ${node} is not both an etcd and control-plane inventory member"
done
export SURVIVOR
# strenv() is evaluated by yq, not the shell.
# shellcheck disable=SC2016
yq -i '(.all.children.etcd.hosts as $hosts | .all.children.etcd.hosts = ({(strenv(SURVIVOR)): $hosts[strenv(SURVIVOR)]} * $hosts))' "$RECOVERY_INVENTORY"
# shellcheck disable=SC2016
yq -i '(.all.children.kube_control_plane.hosts as $hosts | .all.children.kube_control_plane.hosts = ({(strenv(SURVIVOR)): $hosts[strenv(SURVIVOR)]} * $hosts))' "$RECOVERY_INVENTORY"
yq -i '.all.children.broken_etcd.hosts = {} | .all.children.broken_kube_control_plane.hosts = {}' "$RECOVERY_INVENTORY"
for node in "${BROKEN_NODES[@]}"; do
  export BROKEN_NODE="$node"
  NODE_IP=$(yq -r '.all.hosts[strenv(BROKEN_NODE)].ip // .all.hosts[strenv(BROKEN_NODE)].ansible_host // ""' "$RECOVERY_INVENTORY")
  ETCD_MEMBER_NAME=""
  if [[ -n "$NODE_IP" ]]; then
    ETCD_MEMBER_NAME=$(jq -r --arg ip "$NODE_IP" \
      '.members[]? | select(any(.peerURLs[]?; contains($ip))) | .name' \
      "$BUNDLE_DIR/etcd/members.json" | head -1)
  fi
  if [[ -z "$ETCD_MEMBER_NAME" ]]; then
    ETCD_INDEX=$(yq -r '.all.children.etcd.hosts | keys | to_entries[] | select(.value == strenv(BROKEN_NODE)) | .key + 1' "$RECOVERY_INVENTORY")
    [[ "$ETCD_INDEX" =~ ^[1-9][0-9]*$ ]] || fail "cannot derive etcd member name for ${node}"
    ETCD_MEMBER_NAME="etcd${ETCD_INDEX}"
  fi
  export ETCD_MEMBER_NAME
  # shellcheck disable=SC2016
  yq -i '.all.children.broken_etcd.hosts[strenv(BROKEN_NODE)] = {"etcd_member_name": strenv(ETCD_MEMBER_NAME)} | .all.children.broken_kube_control_plane.hosts[strenv(BROKEN_NODE)] = {}' "$RECOVERY_INVENTORY"
done

phrase="RESTORE_ETCD_${PROJECT}"
if [[ "$DRY_RUN" == true ]]; then
  dry "would run Kubespray recover-control-plane.yml with survivor $SURVIVOR"
  dry "broken nodes: ${BROKEN_NODES[*]}"
  dry "snapshot: $BUNDLE_DIR/etcd/snapshot.db"
  exit 0
fi
if [[ "$CONFIRMATION" != "$phrase" ]]; then
  [[ "$FORCE" == true ]] && fail "--force still requires --confirm $phrase"
  printf 'Type %s to run lost-quorum recovery: ' "$phrase"
  read -r CONFIRMATION
fi
[[ "$CONFIRMATION" == "$phrase" ]] || fail "confirmation did not match $phrase"

KUBESPRAY_DIR="${PROJECT_ROOT}/playbooks/kubespray"
if [[ ! -f "$KUBESPRAY_DIR/recover-control-plane.yml" ]]; then
  KUBESPRAY_DIR="$WORK_DIR/kubespray"
  git clone --depth 1 --branch v2.31.0 https://github.com/kubernetes-sigs/kubespray.git "$KUBESPRAY_DIR"
fi
if [[ ! -x "$KUBESPRAY_DIR/.venv/bin/ansible-playbook" ]]; then
  python3 -m venv "$KUBESPRAY_DIR/.venv"
  "$KUBESPRAY_DIR/.venv/bin/pip" install -r "$KUBESPRAY_DIR/requirements.txt"
fi
cp "$BUNDLE_DIR/config/kubespray-custom.yml" "$WORK_DIR/custom.yml"
mkdir -p "$WORK_DIR/inventory/group_vars/all"
cp "$RECOVERY_INVENTORY" "$WORK_DIR/inventory/hosts.yml"
cp "$WORK_DIR/custom.yml" "$WORK_DIR/inventory/group_vars/all/custom.yml"
unset ANSIBLE_CONFIG ANSIBLE_SSH_ARGS ANSIBLE_SSH_COMMON_ARGS
export ANSIBLE_CONFIG="$KUBESPRAY_DIR/ansible.cfg"
"$KUBESPRAY_DIR/.venv/bin/ansible-playbook" \
  -i "$WORK_DIR/inventory/hosts.yml" \
  --become --become-user=root \
  "$KUBESPRAY_DIR/recover-control-plane.yml" \
  -e "etcd_snapshot=$BUNDLE_DIR/etcd/snapshot.db" \
  -e etcd_retries=20 \
  --limit etcd,kube_control_plane
log "Kubespray control-plane recovery completed; validate the API and then run cluster-restore.sh --mode velero only on a replacement cluster if workload recovery is required"
