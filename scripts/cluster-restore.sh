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
VELERO_BACKUP=""
RECOVERY_INVENTORY_INPUT=""
SURVIVOR=""
BROKEN_NODES=()
CONFIRMATION=""
DRY_RUN=false
FORCE=false
RESTORE_TIMEOUT="${CLUSTER_RESTORE_TIMEOUT_SECONDS:-28800}"

log() { printf '[cluster-restore] %s\n' "$*"; }
fail() { printf '[cluster-restore] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[cluster-restore] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: cluster-restore.sh --archive FILE [OPTIONS]

Modes:
  verify   Decrypt, validate both checksum layers, and inspect the manifest.
  velero   Restore Kubernetes resources and PVC data into a replacement cluster.
  etcd     Recover a lost-quorum Kubespray control plane from the etcd snapshot.

Options:
  --archive FILE          Encrypted .age or .enc cluster bundle
  --mode MODE             verify|velero|etcd (default: verify)
  --identity FILE         age identity file
  --velero-backup NAME    Override the backup name recorded in the bundle
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
    --velero-backup) VELERO_BACKUP="$2"; shift 2 ;;
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

[[ "$MODE" =~ ^(verify|velero|etcd)$ ]] || fail "invalid mode: $MODE"
[[ -n "$ARCHIVE" && -f "$ARCHIVE" ]] || fail "--archive must name an existing file"
for tool in tar jq; do command -v "$tool" >/dev/null || fail "required tool is missing: $tool"; done

WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cluster-restore.XXXXXX")
PLAIN_ARCHIVE="${WORK_DIR}/bundle.tar.gz"
cleanup() { rm -rf "$WORK_DIR"; }
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
if tar -tzf "$PLAIN_ARCHIVE" | grep -Eq '(^/|(^|/)\.\.(/|$))'; then
  fail "archive contains an unsafe path"
fi
tar -C "$WORK_DIR" -xzf "$PLAIN_ARCHIVE"
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
      .schema_version == 1 and
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
else
  log "legacy schema-v1 bundle: no remote completion receipt contract is available"
fi
PROJECT=$(jq -r '.project' "$BUNDLE_DIR/MANIFEST.json")
SOURCE_CONTEXT=$(jq -r '.source_context' "$BUNDLE_DIR/MANIFEST.json")
COMPLETENESS=$(jq -r '.completeness' "$BUNDLE_DIR/MANIFEST.json")
[[ "$COMPLETENESS" == complete ]] || fail "bundle is explicitly marked incomplete"
log "verified bundle $(jq -r '.backup_id' "$BUNDLE_DIR/MANIFEST.json") for project $PROJECT"

if [[ "$MODE" == verify ]]; then
  jq '{backup_id,created_at,project,domain,profile,source_context,completeness,
    velero_backup_name,contains,recovery_dependencies,restore_order}' \
    "$BUNDLE_DIR/MANIFEST.json"
  exit 0
fi

if [[ "$MODE" == velero ]]; then
  command -v kubectl >/dev/null || fail "kubectl is required for Velero restore"
  kubectl cluster-info >/dev/null
  TARGET_CONTEXT=$(kubectl config current-context)
  [[ "$TARGET_CONTEXT" != "$SOURCE_CONTEXT" ]] || fail "Velero restore must target a replacement cluster, not source context $SOURCE_CONTEXT"
  [[ -n "$VELERO_BACKUP" ]] || VELERO_BACKUP=$(jq -r '.velero_backup_name // ""' "$BUNDLE_DIR/MANIFEST.json")
  [[ -n "$VELERO_BACKUP" && "$VELERO_BACKUP" != null ]] || fail "bundle has no Velero backup name"
  kubectl wait backupstoragelocation/default -n velero --for=jsonpath='{.status.phase}'=Available --timeout=300s
  kubectl get backup "$VELERO_BACKUP" -n velero >/dev/null || fail "Velero backup is not synchronized into the target cluster: $VELERO_BACKUP"
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
  kubectl get restore "$RESTORE_NAME" -n velero -o yaml > "${ARCHIVE}.${RESTORE_NAME}.yaml"
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
