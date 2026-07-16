#!/usr/bin/env bash
# Create an encrypted, fail-closed disaster-recovery bundle for the platform.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
OUTPUT_DIR="${PROJECT_ROOT}/cluster-backups"
RECIPIENT="${CLUSTER_BACKUP_AGE_RECIPIENT:-}"
DRY_RUN=false
FORCE=false
RUN_APP_BACKUPS=true
RUN_VELERO_BACKUP=true
ALLOW_INCOMPLETE=false
SKIP_CLOUD=false
SKIP_CONTROL_PLANE=false
BACKUP_TIMEOUT="${CLUSTER_BACKUP_TIMEOUT_SECONDS:-28800}"
SSH_USER="${CLUSTER_BACKUP_SSH_USER:-root}"
SSH_BASTION="${CLUSTER_BACKUP_BASTION_HOST:-}"
CONTROL_PLANE_HOST="${CLUSTER_BACKUP_CONTROL_PLANE_HOST:-}"

log() { printf '[cluster-backup] %s\n' "$*"; }
fail() { printf '[cluster-backup] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[cluster-backup] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: cluster-backup.sh [OPTIONS]

Create one encrypted recovery bundle containing the desired platform config,
generated secrets, Kubespray inventory, Helm state, Kubernetes API exports,
an etcd snapshot, control-plane PKI, Hetzner state, and backup identifiers.
Velero separately writes all Kubernetes resources and mounted PVC contents to
the configured external disaster-recovery bucket.

Options:
  --config FILE              Platform YAML (default: platform-orchestrator/platform.yaml)
  --output-dir DIR           Local encrypted bundle directory
  --recipient AGE_RECIPIENT  Encrypt with age instead of passphrase OpenSSL
  --ssh-bastion HOST         Bastion address (otherwise read infra facts)
  --control-plane-host HOST  First control-plane private address
  --ssh-user USER            SSH user (default: root)
  --skip-app-backups         Do not trigger application-native backups
  --skip-velero              Do not trigger Velero resource/PVC backup
  --skip-cloud               Do not capture Hetzner state
  --skip-control-plane       Do not capture etcd and control-plane PKI
  --allow-incomplete         Permit an explicitly degraded bundle with skips
  --dry-run                  Validate arguments and show the workflow
  --force                    Do not ask for confirmation
  -h, --help                 Show this help

Encryption:
  Set CLUSTER_BACKUP_PASSPHRASE, or pass --recipient and install age.
  Passphrases are read only from the environment and never accepted on argv.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --recipient) RECIPIENT="$2"; shift 2 ;;
    --ssh-bastion) SSH_BASTION="$2"; shift 2 ;;
    --control-plane-host) CONTROL_PLANE_HOST="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --skip-app-backups) RUN_APP_BACKUPS=false; shift ;;
    --skip-velero) RUN_VELERO_BACKUP=false; shift ;;
    --skip-cloud) SKIP_CLOUD=true; shift ;;
    --skip-control-plane) SKIP_CONTROL_PLANE=true; shift ;;
    --allow-incomplete) ALLOW_INCOMPLETE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

if [[ "$ALLOW_INCOMPLETE" != true ]] && {
  [[ "$RUN_APP_BACKUPS" != true ]] || [[ "$RUN_VELERO_BACKUP" != true ]] ||
  [[ "$SKIP_CLOUD" == true ]] || [[ "$SKIP_CONTROL_PLANE" == true ]];
}; then
  fail "skip options require --allow-incomplete"
fi

[[ -f "$CONFIG_FILE" ]] || fail "platform config not found: $CONFIG_FILE"
PROJECT=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
DOMAIN=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
PROFILE=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
VELERO_TTL_HOURS=$(yq -r '.backup.disaster_recovery.retention_hours // 720' "$CONFIG_FILE")
[[ "$VELERO_TTL_HOURS" =~ ^[1-9][0-9]*$ ]] || fail "backup DR retention_hours must be a positive integer"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ID="${PROJECT}-cluster-${TIMESTAMP}"

if [[ "$DRY_RUN" == true ]]; then
  dry "would validate cluster, Helm, external Velero storage, SSH, and Hetzner access"
  dry "would trigger application backups: $RUN_APP_BACKUPS"
  dry "would trigger Velero resource and filesystem backup: $RUN_VELERO_BACKUP"
  dry "would capture etcd and control-plane PKI: $([[ "$SKIP_CONTROL_PLANE" == true ]] && echo false || echo true)"
  dry "would capture Hetzner state: $([[ "$SKIP_CLOUD" == true ]] && echo false || echo true)"
  dry "would write encrypted bundle under: $OUTPUT_DIR"
  exit 0
fi

for tool in kubectl helm jq yq tar git comm; do
  command -v "$tool" >/dev/null || fail "required tool is missing: $tool"
done
if [[ -n "$RECIPIENT" ]]; then
  command -v age >/dev/null || fail "age is required for --recipient encryption"
elif [[ -z "${CLUSTER_BACKUP_PASSPHRASE:-}" ]]; then
  fail "set CLUSTER_BACKUP_PASSPHRASE or use --recipient"
else
  command -v openssl >/dev/null || fail "openssl is required for passphrase encryption"
fi

kubectl cluster-info >/dev/null
helm list --all-namespaces >/dev/null
CONTEXT=$(kubectl config current-context)
[[ -n "$CONTEXT" ]] || fail "kubectl has no current context"

if [[ "$FORCE" != true ]]; then
  printf 'Back up cluster context %s for project %s? Type BACKUP: ' "$CONTEXT" "$PROJECT"
  read -r confirmation
  [[ "$confirmation" == BACKUP ]] || fail "confirmation did not match BACKUP"
fi

umask 077
mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"
WORK_DIR=$(mktemp -d "${TMPDIR:-/tmp}/cluster-backup.XXXXXX")
STAGE_DIR="${WORK_DIR}/${BACKUP_ID}"
PLAIN_ARCHIVE="${WORK_DIR}/${BACKUP_ID}.tar.gz"
mkdir -p "$STAGE_DIR"/{config,cluster/resources/namespaced,cluster/resources/cluster,etcd,control-plane,helm,cloud,application-backups}
cleanup() { rm -rf "$WORK_DIR"; }
trap cleanup EXIT INT TERM

sha256_file() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}

copy_required() {
  local source="$1" destination="$2"
  [[ -f "$source" ]] || fail "required recovery input is missing: $source"
  cp "$source" "$destination"
}

log "capturing local desired state"
copy_required "$CONFIG_FILE" "$STAGE_DIR/config/platform.yaml"
copy_required "$PROJECT_ROOT/.platform-secrets.yml" "$STAGE_DIR/config/platform-secrets.yml"
INFRA_FACTS="${PROJECT_ROOT}/${PROJECT}-infra-facts.yml"
copy_required "$INFRA_FACTS" "$STAGE_DIR/config/infra-facts.yml"
KUBESPRAY_INVENTORY="${PROJECT_ROOT}/playbooks/kubespray/inventory/${PROJECT}/hosts.yml"
[[ -f "$KUBESPRAY_INVENTORY" ]] || KUBESPRAY_INVENTORY="${PROJECT_ROOT}/kubespray/inventory/${PROJECT}/hosts.yml"
copy_required "$KUBESPRAY_INVENTORY" "$STAGE_DIR/config/kubespray-hosts.yml"
KUBESPRAY_CUSTOM="$(dirname "$KUBESPRAY_INVENTORY")/group_vars/all/custom.yml"
copy_required "$KUBESPRAY_CUSTOM" "$STAGE_DIR/config/kubespray-custom.yml"
kubectl config view --raw --flatten > "$STAGE_DIR/config/admin.kubeconfig"
[[ -s "$STAGE_DIR/config/admin.kubeconfig" ]] || fail "flattened admin kubeconfig is empty"
git -C "$PROJECT_ROOT" bundle create "$STAGE_DIR/config/repository.bundle" HEAD
git -C "$PROJECT_ROOT" status --porcelain=v1 > "$STAGE_DIR/config/git-status.txt"
git -C "$PROJECT_ROOT" diff --binary > "$STAGE_DIR/config/worktree.patch"
git -C "$PROJECT_ROOT" rev-parse HEAD > "$STAGE_DIR/config/git-revision.txt"

log "capturing Helm release state"
helm list --all-namespaces --output json > "$STAGE_DIR/helm/releases.json"
while IFS=$'\t' read -r namespace release revision; do
  [[ -n "$release" ]] || continue
  safe_name="${namespace}-${release}"
  helm get values "$release" -n "$namespace" --all > "$STAGE_DIR/helm/${safe_name}-values.yaml"
  helm get manifest "$release" -n "$namespace" > "$STAGE_DIR/helm/${safe_name}-manifest.yaml"
  helm get hooks "$release" -n "$namespace" > "$STAGE_DIR/helm/${safe_name}-hooks.yaml"
  printf '%s\t%s\t%s\n' "$namespace" "$release" "$revision" >> "$STAGE_DIR/helm/revisions.tsv"
done < <(jq -r '.[] | [.namespace,.name,(.revision|tostring)] | @tsv' "$STAGE_DIR/helm/releases.json")

log "capturing Kubernetes API resources"
kubectl api-resources --verbs=list --namespaced=true -o name | sort -u > "$STAGE_DIR/cluster/namespaced-api-resources.txt"
kubectl api-resources --verbs=list --namespaced=false -o name | sort -u > "$STAGE_DIR/cluster/cluster-api-resources.txt"
RESOURCE_FAILURES=0
while IFS= read -r resource; do
  case "$resource" in
    events|events.events.k8s.io|bindings|tokenreviews.authentication.k8s.io|subjectaccessreviews.authorization.k8s.io|selfsubjectaccessreviews.authorization.k8s.io|selfsubjectrulesreviews.authorization.k8s.io|localsubjectaccessreviews.authorization.k8s.io) continue ;;
  esac
  filename=${resource//\//_}
  if ! kubectl get "$resource" --all-namespaces -o yaml > "$STAGE_DIR/cluster/resources/namespaced/${filename}.yaml" 2>> "$STAGE_DIR/cluster/export-errors.log"; then
    RESOURCE_FAILURES=$((RESOURCE_FAILURES + 1))
  fi
done < "$STAGE_DIR/cluster/namespaced-api-resources.txt"
while IFS= read -r resource; do
  case "$resource" in
    componentstatuses|tokenreviews.authentication.k8s.io|subjectaccessreviews.authorization.k8s.io|selfsubjectaccessreviews.authorization.k8s.io|selfsubjectrulesreviews.authorization.k8s.io) continue ;;
  esac
  filename=${resource//\//_}
  if ! kubectl get "$resource" -o yaml > "$STAGE_DIR/cluster/resources/cluster/${filename}.yaml" 2>> "$STAGE_DIR/cluster/export-errors.log"; then
    RESOURCE_FAILURES=$((RESOURCE_FAILURES + 1))
  fi
done < "$STAGE_DIR/cluster/cluster-api-resources.txt"
kubectl version -o yaml > "$STAGE_DIR/cluster/version.yaml"
kubectl get --raw='/readyz?verbose' > "$STAGE_DIR/cluster/readyz.txt"
if (( RESOURCE_FAILURES > 0 )) && [[ "$ALLOW_INCOMPLETE" != true ]]; then
  fail "$RESOURCE_FAILURES Kubernetes API resource exports failed; see export-errors.log"
fi

build_ssh_args() {
  SSH_ARGS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20)
  [[ -n "$SSH_BASTION" ]] && SSH_ARGS+=(-J "${SSH_USER}@${SSH_BASTION}")
}

if [[ "$SKIP_CONTROL_PLANE" != true ]]; then
  [[ -n "$SSH_BASTION" ]] || SSH_BASTION=$(yq -r '.bastion_public_ip // ""' "$INFRA_FACTS")
  [[ -n "$CONTROL_PLANE_HOST" ]] || CONTROL_PLANE_HOST=$(yq -r '.first_master_ip // ""' "$INFRA_FACTS")
  [[ -n "$CONTROL_PLANE_HOST" ]] || fail "first control-plane host is absent from infra facts"
  build_ssh_args
  log "capturing verified etcd snapshot from $CONTROL_PLANE_HOST"
  ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${CONTROL_PLANE_HOST}" \
    'set -eu; . /etc/etcd.env; export ETCDCTL_API=3 ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl endpoint health --cluster' \
    > "$STAGE_DIR/etcd/endpoint-health.txt"
  # shellcheck disable=SC2029
  ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${CONTROL_PLANE_HOST}" \
    "set -eu; . /etc/etcd.env; export ETCDCTL_API=3 ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl snapshot save /tmp/${BACKUP_ID}.db >/dev/null; if command -v etcdutl >/dev/null; then etcdutl --write-out=json snapshot status /tmp/${BACKUP_ID}.db; else etcdctl --write-out=json snapshot status /tmp/${BACKUP_ID}.db; fi" \
    > "$STAGE_DIR/etcd/snapshot-status.json"
  # shellcheck disable=SC2029
  ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${CONTROL_PLANE_HOST}" \
    "cat /tmp/${BACKUP_ID}.db && rm -f /tmp/${BACKUP_ID}.db" > "$STAGE_DIR/etcd/snapshot.db"
  [[ -s "$STAGE_DIR/etcd/snapshot.db" ]] || fail "etcd snapshot is empty"
  ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${CONTROL_PLANE_HOST}" \
    'set -eu; . /etc/etcd.env; export ETCDCTL_API=3 ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl --write-out=json member list' \
    > "$STAGE_DIR/etcd/members.json"

  log "capturing control-plane PKI and static configuration"
  while IFS=$'\t' read -r node host; do
    [[ -n "$host" ]] || continue
    ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${host}" \
      'tar --numeric-owner -C / -czf - etc/kubernetes etc/ssl/etcd etc/etcd.env' \
      > "$STAGE_DIR/control-plane/${node}.tar.gz"
    [[ -s "$STAGE_DIR/control-plane/${node}.tar.gz" ]] || fail "empty control-plane archive for $node"
  done < <(yq -r '.master_ips | to_entries | .[] | [.key,.value] | @tsv' "$INFRA_FACTS")
fi

if [[ "$SKIP_CLOUD" != true ]]; then
  command -v hcloud >/dev/null || fail "hcloud is required for cloud-state capture"
  [[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required for cloud-state capture"
  log "capturing Hetzner infrastructure state"
  hcloud version > "$STAGE_DIR/cloud/hcloud-version.txt"
  hcloud server list --selector "project=${PROJECT}" -o json > "$STAGE_DIR/cloud/servers.json"
  for spec in "network:${PROJECT}-network" "firewall:${PROJECT}-fw-bastion" "firewall:${PROJECT}-fw-nodes" \
    "load-balancer:${PROJECT}-lb" "placement-group:${PROJECT}-spread" "ssh-key:${PROJECT}-key" "zone:${DOMAIN}"; do
    kind=${spec%%:*}; name=${spec#*:}; safe_name=${name//[^[:alnum:]._-]/_}
    file="${kind//-/_}-${safe_name}"
    describe_args=()
    [[ "$kind" != load-balancer ]] || describe_args+=(--expand-targets)
    if ! hcloud "$kind" describe "$name" "${describe_args[@]}" -o json > "$STAGE_DIR/cloud/${file}.json" 2> "$STAGE_DIR/cloud/${file}.error"; then
      printf '{"state":"absent","name":"%s"}\n' "$name" > "$STAGE_DIR/cloud/${file}.json"
    fi
  done
  if hcloud zone describe "$DOMAIN" >/dev/null 2>&1; then
    hcloud zone rrset list "$DOMAIN" -o json > "$STAGE_DIR/cloud/zone-rrsets.json"
  fi
  : > "$STAGE_DIR/cloud/volumes.jsonl"
  while IFS= read -r volume_id; do
    [[ -n "$volume_id" ]] || continue
    hcloud volume describe "$volume_id" -o json >> "$STAGE_DIR/cloud/volumes.jsonl"
  done < <(kubectl get pv -o json | jq -r '.items[] | select(.spec.csi.driver=="csi.hetzner.cloud") | .spec.csi.volumeHandle')
fi

APP_BACKUP_RESULT=skipped
if [[ "$RUN_APP_BACKUPS" == true ]]; then
  log "triggering application-consistent backups"
  PROJECT_NAME="$PROJECT" "$SCRIPT_DIR/backup-all.sh" --config "$CONFIG_FILE" --force \
    | tee "$STAGE_DIR/application-backups/backup-all.log"
  APP_BACKUP_RESULT=completed
fi

VELERO_BACKUP_NAME=""
VELERO_BACKUP_RESULT=skipped
if [[ "$RUN_VELERO_BACKUP" == true ]]; then
  log "triggering external Velero resource and PVC backup"
  kubectl wait backupstoragelocation/default -n velero --for=jsonpath='{.status.phase}'=Available --timeout=300s
  kubectl get pods --all-namespaces -o json | jq -r '
    .items[] as $pod
    | $pod.spec.volumes[]?
    | select(.persistentVolumeClaim != null)
    | [$pod.metadata.namespace, $pod.metadata.name, .name]
    | @tsv' | sort -u > "$STAGE_DIR/application-backups/mounted-pod-volumes.expected.tsv"
  VELERO_BACKUP_NAME="${BACKUP_ID,,}"
  VELERO_BACKUP_NAME=${VELERO_BACKUP_NAME//_/-}
  kubectl apply -f - <<EOF
apiVersion: velero.io/v1
kind: Backup
metadata:
  name: ${VELERO_BACKUP_NAME}
  namespace: velero
  labels:
    backup.platform.io/project: ${PROJECT}
spec:
  includedNamespaces:
    - '*'
  includeClusterResources: true
  defaultVolumesToFsBackup: true
  storageLocation: default
  ttl: ${VELERO_TTL_HOURS}h
EOF
  deadline=$((SECONDS + BACKUP_TIMEOUT))
  while (( SECONDS < deadline )); do
    phase=$(kubectl get backup "$VELERO_BACKUP_NAME" -n velero -o jsonpath='{.status.phase}' 2>/dev/null || echo New)
    case "$phase" in
      Completed) VELERO_BACKUP_RESULT=completed; break ;;
      Failed|PartiallyFailed|FailedValidation) kubectl get backup "$VELERO_BACKUP_NAME" -n velero -o yaml > "$STAGE_DIR/application-backups/velero-backup.yaml"; fail "Velero backup ended in phase $phase" ;;
    esac
    sleep 15
  done
  [[ "$VELERO_BACKUP_RESULT" == completed ]] || fail "Velero backup timed out"
  kubectl get backup "$VELERO_BACKUP_NAME" -n velero -o yaml > "$STAGE_DIR/application-backups/velero-backup.yaml"
  kubectl get podvolumebackups -n velero -l "velero.io/backup-name=${VELERO_BACKUP_NAME}" -o json \
    > "$STAGE_DIR/application-backups/pod-volume-backups.json"
  jq -e '[.items[] | select((.status.phase // "") != "Completed")] | length == 0' \
    "$STAGE_DIR/application-backups/pod-volume-backups.json" >/dev/null \
    || fail "one or more Velero filesystem backups did not complete"
  jq -r '.items[] | [.spec.pod.namespace, .spec.pod.name, .spec.volume] | @tsv' \
    "$STAGE_DIR/application-backups/pod-volume-backups.json" | sort -u \
    > "$STAGE_DIR/application-backups/mounted-pod-volumes.completed.tsv"
  comm -23 \
    "$STAGE_DIR/application-backups/mounted-pod-volumes.expected.tsv" \
    "$STAGE_DIR/application-backups/mounted-pod-volumes.completed.tsv" \
    > "$STAGE_DIR/application-backups/mounted-pod-volumes.missing.tsv"
  [[ ! -s "$STAGE_DIR/application-backups/mounted-pod-volumes.missing.tsv" ]] \
    || fail "Velero did not create a completed filesystem backup for every mounted PVC volume"
fi

COMPLETENESS=complete
[[ "$ALLOW_INCOMPLETE" == true ]] && COMPLETENESS=incomplete
HAS_CONTROL_PLANE=true
HAS_CLOUD=true
[[ "$SKIP_CONTROL_PLANE" == true ]] && HAS_CONTROL_PLANE=false
[[ "$SKIP_CLOUD" == true ]] && HAS_CLOUD=false
jq -n \
  --arg id "$BACKUP_ID" --arg timestamp "$TIMESTAMP" --arg project "$PROJECT" \
  --arg domain "$DOMAIN" --arg profile "$PROFILE" --arg context "$CONTEXT" \
  --arg completeness "$COMPLETENESS" --arg app "$APP_BACKUP_RESULT" \
  --arg velero "$VELERO_BACKUP_RESULT" --arg veleroName "$VELERO_BACKUP_NAME" \
  --argjson resourceFailures "$RESOURCE_FAILURES" --argjson hasControlPlane "$HAS_CONTROL_PLANE" \
  --argjson hasCloud "$HAS_CLOUD" \
  '{schema_version:1,backup_id:$id,created_at:$timestamp,project:$project,domain:$domain,
    profile:$profile,source_context:$context,completeness:$completeness,
    application_backups:$app,velero_backup:$velero,velero_backup_name:$veleroName,
    kubernetes_resource_export_failures:$resourceFailures,
    contains:{platform_config:true,generated_secrets:true,kubespray_inventory:true,
      helm_state:true,kubernetes_api_exports:true,etcd_snapshot:$hasControlPlane,
      control_plane_pki:$hasControlPlane,cloud_state:$hasCloud},
    restore_order:["infrastructure","control-plane","velero","application-data","health-gates"]}' \
  > "$STAGE_DIR/MANIFEST.json"

log "checksumming and encrypting recovery bundle"
(
  cd "$STAGE_DIR"
  checksum_tmp=$(mktemp)
  find . -type f ! -name SHA256SUMS -print0 | sort -z | while IFS= read -r -d '' file; do
    if command -v sha256sum >/dev/null; then sha256sum "$file"; else shasum -a 256 "$file"; fi
  done > "$checksum_tmp"
  mv "$checksum_tmp" SHA256SUMS
)
tar -C "$WORK_DIR" -czf "$PLAIN_ARCHIVE" "$BACKUP_ID"
if [[ -n "$RECIPIENT" ]]; then
  FINAL_ARCHIVE="${OUTPUT_DIR}/${BACKUP_ID}.tar.gz.age"
  age -r "$RECIPIENT" -o "$FINAL_ARCHIVE" "$PLAIN_ARCHIVE"
  ENCRYPTION=age
else
  FINAL_ARCHIVE="${OUTPUT_DIR}/${BACKUP_ID}.tar.gz.enc"
  openssl enc -aes-256-cbc -pbkdf2 -iter 600000 -salt \
    -pass env:CLUSTER_BACKUP_PASSPHRASE -in "$PLAIN_ARCHIVE" -out "$FINAL_ARCHIVE"
  ENCRYPTION=openssl-aes-256-cbc-pbkdf2
fi
chmod 600 "$FINAL_ARCHIVE"
sha256_file "$FINAL_ARCHIVE" > "${FINAL_ARCHIVE}.sha256"
jq -n --arg backupId "$BACKUP_ID" --arg archive "$(basename "$FINAL_ARCHIVE")" \
  --arg encryption "$ENCRYPTION" --arg completeness "$COMPLETENESS" \
  --arg veleroBackup "$VELERO_BACKUP_NAME" \
  '{backup_id:$backupId,archive:$archive,encryption:$encryption,
    completeness:$completeness,velero_backup_name:$veleroBackup}' \
  > "${FINAL_ARCHIVE}.manifest.json"
chmod 600 "${FINAL_ARCHIVE}.sha256" "${FINAL_ARCHIVE}.manifest.json"

log "backup complete: $FINAL_ARCHIVE"
log "checksum: ${FINAL_ARCHIVE}.sha256"
log "Velero backup: ${VELERO_BACKUP_NAME:-not-requested}"
