#!/usr/bin/env bash
# Create an encrypted, fail-closed disaster-recovery bundle for the platform.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
if [[ -x "${PROJECT_ROOT}/.venv/bin/aws" ]]; then
  PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"
  export PATH
fi
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
SECRETS_FILE=""
VAULT_INIT_FILE=""
OUTPUT_DIR="${PROJECT_ROOT}/cluster-backups"
RECIPIENT="${CLUSTER_BACKUP_AGE_RECIPIENT:-}"
DRY_RUN=false
FORCE=false
RUN_APP_BACKUPS=true
RUN_VELERO_BACKUP=true
RUN_REMOTE_PUBLISH=true
ALLOW_INCOMPLETE=false
SKIP_CLOUD=false
SKIP_CONTROL_PLANE=false
BACKUP_TIMEOUT="${CLUSTER_BACKUP_TIMEOUT_SECONDS:-28800}"
SSH_USER="${CLUSTER_BACKUP_SSH_USER:-root}"
SSH_BASTION="${CLUSTER_BACKUP_BASTION_HOST:-}"
CONTROL_PLANE_HOST="${CLUSTER_BACKUP_CONTROL_PLANE_HOST:-}"
SSH_IDENTITY="${CLUSTER_BACKUP_SSH_IDENTITY:-}"
SSH_KNOWN_HOSTS_FILE="${CLUSTER_BACKUP_SSH_KNOWN_HOSTS_FILE:-}"
DR_ENDPOINT="${BACKUP_DR_ENDPOINT:-}"
DR_BUCKET="${BACKUP_DR_BUCKET:-}"
DR_PREFIX="${CLUSTER_BACKUP_DR_PREFIX:-}"
DR_REGION="${BACKUP_DR_REGION:-us-east-1}"

log() { printf '[cluster-backup] %s\n' "$*"; }
fail() { printf '[cluster-backup] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[cluster-backup] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: cluster-backup.sh [OPTIONS]

Create one encrypted recovery bundle containing the desired platform config,
generated secrets, Ansible-Vault-encrypted Vault initialization material,
Kubespray inventory, Helm state, Kubernetes API exports, an etcd snapshot,
control-plane PKI, Hetzner state, and backup identifiers.
Velero separately writes all Kubernetes resources and mounted PVC contents to
the configured external disaster-recovery bucket. The encrypted bundle is
published to that bucket and downloaded again for a SHA-256 verification;
its JSON manifest is uploaded last as the remote completion receipt.

Options:
  --config FILE              Platform YAML (default: platform-orchestrator/platform.yaml)
  --secrets-file FILE        Generated secrets for this exact cluster
  --vault-init-file FILE     Exact Ansible-Vault-encrypted Vault init file;
                             required when secrets/Vault is enabled
  --output-dir DIR           Local encrypted bundle directory
  --recipient AGE_RECIPIENT  Encrypt with age instead of passphrase OpenSSL
  --ssh-bastion HOST         Bastion address (otherwise read infra facts)
  --control-plane-host HOST  First control-plane private address
  --ssh-user USER            SSH user (default: root)
  --ssh-identity FILE        SSH private key (profile/defaults fallback)
  --ssh-known-hosts FILE     Isolated persistent SSH trust file (optional)
  --skip-app-backups         Do not trigger application-native backups
  --skip-velero              Do not trigger Velero resource/PVC backup
  --skip-remote-publish      Keep the encrypted bundle local only
  --skip-cloud               Do not capture Hetzner state
  --skip-control-plane       Do not capture etcd and control-plane PKI
  --allow-incomplete         Permit an explicitly degraded bundle with skips
  --dry-run                  Validate arguments and show the workflow
  --force                    Do not ask for confirmation
  -h, --help                 Show this help

Encryption:
  Set CLUSTER_BACKUP_PASSPHRASE, or pass --recipient and install age.
  Passphrases are read only from the environment and never accepted on argv.
  Remote publishing uses BACKUP_DR_ENDPOINT, BACKUP_DR_BUCKET,
  BACKUP_DR_ACCESS_KEY, and BACKUP_DR_SECRET_KEY from the environment/.env.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --secrets-file) SECRETS_FILE="$2"; shift 2 ;;
    --vault-init-file) VAULT_INIT_FILE="$2"; shift 2 ;;
    --output-dir) OUTPUT_DIR="$2"; shift 2 ;;
    --recipient) RECIPIENT="$2"; shift 2 ;;
    --ssh-bastion) SSH_BASTION="$2"; shift 2 ;;
    --control-plane-host) CONTROL_PLANE_HOST="$2"; shift 2 ;;
    --ssh-user) SSH_USER="$2"; shift 2 ;;
    --ssh-identity) SSH_IDENTITY="$2"; shift 2 ;;
    --ssh-known-hosts) SSH_KNOWN_HOSTS_FILE="$2"; shift 2 ;;
    --skip-app-backups) RUN_APP_BACKUPS=false; shift ;;
    --skip-velero) RUN_VELERO_BACKUP=false; shift ;;
    --skip-remote-publish) RUN_REMOTE_PUBLISH=false; shift ;;
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
  [[ "$RUN_REMOTE_PUBLISH" != true ]] ||
  [[ "$SKIP_CLOUD" == true ]] || [[ "$SKIP_CONTROL_PLANE" == true ]];
}; then
  fail "skip options require --allow-incomplete"
fi

[[ -f "$CONFIG_FILE" ]] || fail "platform config not found: $CONFIG_FILE"
[[ -n "$SECRETS_FILE" ]] || SECRETS_FILE="${PROJECT_ROOT}/playbooks/.platform-secrets.yml"
[[ -f "$SECRETS_FILE" ]] || fail "generated secrets file not found: $SECRETS_FILE"
PROJECT=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
VAULT_INIT_REQUIRED=$(yq -r '.secrets.enabled // false' "$CONFIG_FILE")
[[ "$VAULT_INIT_REQUIRED" =~ ^(true|false)$ ]] \
  || fail "secrets.enabled must resolve to true or false"
VAULT_INIT_INCLUDED=false
if [[ "$VAULT_INIT_REQUIRED" == true || -n "$VAULT_INIT_FILE" ]]; then
  [[ -n "$VAULT_INIT_FILE" ]] \
    || fail "--vault-init-file is required when secrets/Vault is enabled"
  [[ -f "$VAULT_INIT_FILE" && -r "$VAULT_INIT_FILE" && -s "$VAULT_INIT_FILE" ]] \
    || fail "exact Vault init file is missing, unreadable, or empty"
  command -v ansible-vault >/dev/null \
    || fail "ansible-vault is required to validate Vault initialization material"
  command -v jq >/dev/null || fail "jq is required to validate Vault initialization material"
  [[ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" \
    && -f "${ANSIBLE_VAULT_PASSWORD_FILE}" \
    && -r "${ANSIBLE_VAULT_PASSWORD_FILE}" \
    && -s "${ANSIBLE_VAULT_PASSWORD_FILE}" ]] \
    || fail "ANSIBLE_VAULT_PASSWORD_FILE must name a readable, non-empty file"
  IFS= read -r vault_init_header < "$VAULT_INIT_FILE" || true
  [[ "$vault_init_header" == "\$ANSIBLE_VAULT;"* ]] \
    || fail "Vault initialization material is not Ansible Vault encrypted"
  if ! ansible-vault view \
    --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE" \
    "$VAULT_INIT_FILE" 2>/dev/null \
    | jq -e '
        (.root_token | type == "string" and length > 0) and
        (.unseal_keys_b64 | type == "array" and length > 0) and
        (all(.unseal_keys_b64[]; type == "string" and length > 0))
      ' >/dev/null 2>&1; then
    fail "Vault initialization material failed encrypted structure validation"
  fi
  VAULT_INIT_INCLUDED=true
fi
DOMAIN=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
DNS_ZONE=$(yq -r '.hetzner_dns_zone // .global.domain // ""' "$CONFIG_FILE")
[[ -n "$DNS_ZONE" ]] || DNS_ZONE="$DOMAIN"
if [[ "$DOMAIN" == "$DNS_ZONE" ]]; then
  DNS_RECORD_ROOT=@
elif [[ "$DOMAIN" == *."$DNS_ZONE" ]]; then
  DNS_RECORD_ROOT="${DOMAIN%."$DNS_ZONE"}"
else
  fail "global.domain must equal or be a subdomain of hetzner_dns_zone"
fi
PROFILE=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
BASTION_SERVER_TYPE=$(yq -r '.network.bastion.server_type // ""' "$CONFIG_FILE")
CONTROL_PLANE_SERVER_TYPE=$(yq -r '.infrastructure.control_plane.type // ""' "$CONFIG_FILE")
WORKER_SERVER_TYPE=$(yq -r '.infrastructure.workers.type // ""' "$CONFIG_FILE")
[[ -n "$DR_ENDPOINT" ]] || DR_ENDPOINT=$(yq -r '.backup.disaster_recovery.endpoint // ""' "$CONFIG_FILE")
[[ -n "$DR_BUCKET" ]] || DR_BUCKET=$(yq -r '.backup.disaster_recovery.bucket // ""' "$CONFIG_FILE")
VELERO_DR_PREFIX=$(yq -r '.backup.disaster_recovery.prefix // "k8s/velero"' "$CONFIG_FILE")
VELERO_DR_PREFIX="${VELERO_DR_PREFIX#/}"
VELERO_DR_PREFIX="${VELERO_DR_PREFIX%/}"
if [[ -z "$DR_PREFIX" ]]; then
  DR_PREFIX_PARENT="${VELERO_DR_PREFIX%/*}"
  [[ "$DR_PREFIX_PARENT" != "$VELERO_DR_PREFIX" ]] || DR_PREFIX_PARENT=""
  DR_PREFIX="${DR_PREFIX_PARENT:+${DR_PREFIX_PARENT}/}cluster-bundles/${PROJECT}"
fi
DR_PREFIX="${DR_PREFIX#/}"
DR_PREFIX="${DR_PREFIX%/}"
if [[ "$DR_PREFIX" == "$VELERO_DR_PREFIX" || "$DR_PREFIX" == "$VELERO_DR_PREFIX/"* ]]; then
  fail "cluster bundle prefix must be outside the Velero storage prefix (${VELERO_DR_PREFIX})"
fi
if [[ -z "$SSH_IDENTITY" ]]; then
  SSH_IDENTITY=$(yq -r '.infrastructure.ssh_key_path // ""' "$CONFIG_FILE")
fi
if [[ -z "$SSH_IDENTITY" && -f "$PROJECT_ROOT/defaults/main.yml" ]]; then
  SSH_IDENTITY=$(yq -r '.ssh_key_path // ""' "$PROJECT_ROOT/defaults/main.yml")
fi
if [[ "${SSH_IDENTITY#\~/}" != "$SSH_IDENTITY" ]]; then
  SSH_IDENTITY="${HOME:?HOME is required to resolve the SSH identity}/${SSH_IDENTITY:2}"
fi
VELERO_TTL_HOURS=$(yq -r '.backup.disaster_recovery.retention_hours // 720' "$CONFIG_FILE")
[[ "$VELERO_TTL_HOURS" =~ ^[1-9][0-9]*$ ]] || fail "backup DR retention_hours must be a positive integer"
TIMESTAMP=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP_ID="${PROJECT}-cluster-${TIMESTAMP}"

if [[ "$DRY_RUN" == true ]]; then
  dry "would validate cluster, Helm, external Velero storage, SSH, and Hetzner access"
  dry "would trigger application backups: $RUN_APP_BACKUPS"
  dry "would trigger Velero resource and filesystem backup: $RUN_VELERO_BACKUP"
  dry "would publish and download-verify the encrypted bundle in external DR storage: $RUN_REMOTE_PUBLISH"
  dry "would capture etcd and control-plane PKI: $([[ "$SKIP_CONTROL_PLANE" == true ]] && echo false || echo true)"
  dry "would capture Hetzner state: $([[ "$SKIP_CLOUD" == true ]] && echo false || echo true)"
  dry "would include validated Ansible Vault-encrypted Vault initialization material: $VAULT_INIT_INCLUDED"
  dry "would write encrypted bundle under: $OUTPUT_DIR"
  exit 0
fi

for tool in kubectl helm jq yq tar git comm; do
  command -v "$tool" >/dev/null || fail "required tool is missing: $tool"
done
if [[ "$RUN_REMOTE_PUBLISH" == true ]]; then
  command -v aws >/dev/null || fail "required tool is missing: aws"
  command -v cmp >/dev/null || fail "required tool is missing: cmp"
  [[ -n "$DR_ENDPOINT" ]] || fail "BACKUP_DR_ENDPOINT or backup.disaster_recovery.endpoint is required"
  [[ -n "$DR_BUCKET" ]] || fail "BACKUP_DR_BUCKET or backup.disaster_recovery.bucket is required"
  [[ -n "$DR_PREFIX" && "$DR_PREFIX" != *..* ]] || fail "CLUSTER_BACKUP_DR_PREFIX must be a non-empty safe object prefix"
  [[ -n "${BACKUP_DR_ACCESS_KEY:-}" ]] || fail "BACKUP_DR_ACCESS_KEY is required for remote bundle publishing"
  [[ -n "${BACKUP_DR_SECRET_KEY:-}" ]] || fail "BACKUP_DR_SECRET_KEY is required for remote bundle publishing"
fi

run_with_retry() {
  local attempt
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$@"; then
      return 0
    fi
    sleep $((attempt * 2))
  done
  return 1
}

if [[ -n "$RECIPIENT" ]]; then
  command -v age >/dev/null || fail "age is required for --recipient encryption"
elif [[ -z "${CLUSTER_BACKUP_PASSPHRASE:-}" ]]; then
  fail "set CLUSTER_BACKUP_PASSPHRASE or use --recipient"
else
  command -v openssl >/dev/null || fail "openssl is required for passphrase encryption"
fi

run_with_retry kubectl cluster-info >/dev/null \
  || fail "Kubernetes API preflight failed after retries"
run_with_retry helm list --all-namespaces >/dev/null \
  || fail "Helm API preflight failed after retries"
CONTEXT=$(kubectl config current-context)
[[ -n "$CONTEXT" ]] || fail "kubectl has no current context"
SOURCE_CLUSTER_UID=$(run_with_retry kubectl get namespace kube-system -o jsonpath='{.metadata.uid}') \
  || fail "could not capture the source cluster UID"
[[ "$SOURCE_CLUSTER_UID" =~ ^[a-f0-9-]{16,}$ ]] \
  || fail "source cluster UID is empty or malformed"

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
POD_ANNOTATIONS_FILE="${WORK_DIR}/velero-pvc-annotations.tsv"
PVC_EVIDENCE_TMP=""
restore_backup_annotations() {
  local namespace pod previous _volumes
  [[ -f "$POD_ANNOTATIONS_FILE" && -s "$POD_ANNOTATIONS_FILE" ]] || return 0
  while IFS=$'\t' read -r namespace pod previous _volumes; do
    if [[ "$previous" == __ABSENT__ ]]; then
      kubectl annotate pod -n "$namespace" "$pod" backup.velero.io/backup-volumes- --overwrite >/dev/null 2>&1 || true
    else
      kubectl annotate pod -n "$namespace" "$pod" "backup.velero.io/backup-volumes=${previous}" --overwrite >/dev/null 2>&1 || true
    fi
  done < "$POD_ANNOTATIONS_FILE"
  [[ -e "$POD_ANNOTATIONS_FILE" ]] && : > "$POD_ANNOTATIONS_FILE"
}
cleanup() {
  restore_backup_annotations
  [[ -z "$PVC_EVIDENCE_TMP" ]] || rm -f "$PVC_EVIDENCE_TMP"
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT INT TERM

sha256_file() {
  if command -v sha256sum >/dev/null; then sha256sum "$1"; else shasum -a 256 "$1"; fi
}

aws_dr() {
  AWS_ACCESS_KEY_ID="${BACKUP_DR_ACCESS_KEY:?}" \
    AWS_SECRET_ACCESS_KEY="${BACKUP_DR_SECRET_KEY:?}" \
    AWS_DEFAULT_REGION="$DR_REGION" AWS_EC2_METADATA_DISABLED=true \
    aws --endpoint-url "$DR_ENDPOINT" "$@"
}

evaluate_pvc_protection_gate() {
  log "evaluating the complete-backup PVC protection gate"
  PVC_SNAPSHOT="${WORK_DIR}/persistentvolumeclaims.json"
  PVC_PODS_SNAPSHOT="${WORK_DIR}/pvc-pods.json"
  PVC_EVIDENCE="${OUTPUT_DIR}/${BACKUP_ID}.pvc-evidence.json"
  PVC_EVIDENCE_TMP="${PVC_EVIDENCE}.tmp"
  kubectl get persistentvolumeclaims --all-namespaces -o json > "$PVC_SNAPSHOT"
  kubectl get pods --all-namespaces -o json > "$PVC_PODS_SNAPSHOT"
  jq -n --arg backupId "$BACKUP_ID" --arg project "$PROJECT" --arg context "$CONTEXT" \
  --slurpfile pvc "$PVC_SNAPSHOT" --slurpfile pods "$PVC_PODS_SNAPSHOT" '
  [
    $pods[0].items[] as $pod
    | select($pod.metadata.deletionTimestamp == null)
    | select(($pod.status.phase // "") == "Running")
    | ([
        $pod.spec.initContainers[]?,
        $pod.spec.containers[]?,
        $pod.spec.ephemeralContainers[]?
      ] | [.[].volumeMounts[]?.name] | unique) as $mounted_names
    | $pod.spec.volumes[]? as $volume
    | select($volume.persistentVolumeClaim != null)
    | select($mounted_names | index($volume.name))
    | {
        namespace: $pod.metadata.namespace,
        pod: $pod.metadata.name,
        volume: $volume.name,
        claim: $volume.persistentVolumeClaim.claimName
      }
  ] as $mounts
  | [
      $pvc[0].items[]
      | select(.metadata.deletionTimestamp == null)
      | . as $claim
      | [$mounts[] | select(
          .namespace == $claim.metadata.namespace and
          .claim == $claim.metadata.name
        )] as $claim_mounts
      | {
          namespace: $claim.metadata.namespace,
          name: $claim.metadata.name,
          phase: ($claim.status.phase // "Unknown"),
          volume_name: ($claim.spec.volumeName // ""),
          storage_class: ($claim.spec.storageClassName // ""),
          requested_storage: ($claim.spec.resources.requests.storage // ""),
          mounts: $claim_mounts,
          protected: (
            ($claim.status.phase // "") == "Bound" and
            ($claim_mounts | length) > 0
          ),
          failures: ([
            if ($claim.status.phase // "") != "Bound" then "not_bound" else empty end,
            if ($claim_mounts | length) == 0 then "unmounted" else empty end
          ])
        }
    ] | sort_by(.namespace, .name) as $claims
  | {
      schema_version: 1,
      backup_id: $backupId,
      project: $project,
      source_context: $context,
      evaluated_at: (now | todateiso8601),
      policy: "every non-terminating PVC must be Bound and mounted by a non-terminating Running pod",
      status: (if all($claims[]; .protected) then "complete" else "incomplete" end),
      summary: {
        evaluated: ($claims | length),
        protected: ([$claims[] | select(.protected)] | length),
        failures: ([$claims[] | select(.protected | not)] | length)
      },
      claims: $claims
    }
  ' > "$PVC_EVIDENCE_TMP"
  mv "$PVC_EVIDENCE_TMP" "$PVC_EVIDENCE"
  PVC_EVIDENCE_TMP=""
  chmod 600 "$PVC_EVIDENCE"
  cp "$PVC_EVIDENCE" "$STAGE_DIR/application-backups/pvc-protection-evidence.json"
  PVC_GATE_RESULT=$(jq -r '.status' "$PVC_EVIDENCE")
  PVC_GATE_FAILURES=$(jq -r '.summary.failures' "$PVC_EVIDENCE")
  if [[ "$PVC_GATE_RESULT" != complete ]]; then
    if [[ "$ALLOW_INCOMPLETE" != true ]]; then
      fail "$PVC_GATE_FAILURES non-terminating PVC(s) are non-Bound or unmounted; evidence: $PVC_EVIDENCE"
    fi
    log "continuing explicitly incomplete backup with $PVC_GATE_FAILURES unprotected PVC(s); evidence: $PVC_EVIDENCE"
  else
    log "PVC protection gate passed; evidence: $PVC_EVIDENCE"
  fi
}

copy_required() {
  local source="$1" destination="$2"
  [[ -f "$source" ]] || fail "required recovery input is missing: $source"
  cp "$source" "$destination"
}

hcloud_safe() {
  local error_file status attempt
  error_file=$(mktemp "${TMPDIR:-/tmp}/hcloud-error.XXXXXX")
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    : > "$error_file"
    if hcloud "$@" 2>"$error_file"; then
      rm -f "$error_file"
      return 0
    else
      status=$?
    fi
    # Absence is authoritative and should not be retried. Authentication,
    # throttling, network, and service failures are transient or fatal errors;
    # retry them before the caller decides whether the bundle can proceed.
    grep -qi 'not found' "$error_file" && break
    (( attempt == 10 )) || sleep $((attempt * 2))
  done
  sed -E "s/token '[^']+'/token '[REDACTED]'/g" "$error_file" >&2
  rm -f "$error_file"
  return "$status"
}

log "capturing local desired state"
copy_required "$CONFIG_FILE" "$STAGE_DIR/config/platform.yaml"
copy_required "$SECRETS_FILE" "$STAGE_DIR/config/platform-secrets.yml"
if [[ "$VAULT_INIT_INCLUDED" == true ]]; then
  copy_required "$VAULT_INIT_FILE" "$STAGE_DIR/config/vault-init.json.vault"
  chmod 600 "$STAGE_DIR/config/vault-init.json.vault"
fi
INFRA_FACTS="${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml"
copy_required "$INFRA_FACTS" "$STAGE_DIR/config/infra-facts.yml"
KUBESPRAY_INVENTORY="${PROJECT_ROOT}/playbooks/kubespray/inventory/${PROJECT}/hosts.yml"
[[ -f "$KUBESPRAY_INVENTORY" ]] || KUBESPRAY_INVENTORY="${PROJECT_ROOT}/kubespray/inventory/${PROJECT}/hosts.yml"
copy_required "$KUBESPRAY_INVENTORY" "$STAGE_DIR/config/kubespray-hosts.yml"
KUBESPRAY_CUSTOM="$(dirname "$KUBESPRAY_INVENTORY")/group_vars/all/custom.yml"
copy_required "$KUBESPRAY_CUSTOM" "$STAGE_DIR/config/kubespray-custom.yml"
kubectl config view --raw --flatten > "$STAGE_DIR/config/admin.kubeconfig"
[[ -s "$STAGE_DIR/config/admin.kubeconfig" ]] || fail "flattened admin kubeconfig is empty"
"$SCRIPT_DIR/capture-repository-state.sh" "$PROJECT_ROOT" "$STAGE_DIR/config"
REPOSITORY_BUNDLE_SHA256=$(sha256_file "$STAGE_DIR/config/repository.bundle" | awk '{print $1}')
WORKTREE_PATCH_SHA256=$(sha256_file "$STAGE_DIR/config/worktree.patch" | awk '{print $1}')
GIT_REVISION=$(<"$STAGE_DIR/config/git-revision.txt")
GIT_REVISION_SHA256=$(sha256_file "$STAGE_DIR/config/git-revision.txt" | awk '{print $1}')
UNTRACKED_ARCHIVE_SHA256=$(sha256_file "$STAGE_DIR/config/repository-untracked.tar" | awk '{print $1}')
UNTRACKED_PATHS_SHA256=$(sha256_file "$STAGE_DIR/config/repository-untracked-files.txt" | awk '{print $1}')
UNTRACKED_FILE_COUNT=$(<"$STAGE_DIR/config/repository-untracked-count.txt")

log "capturing Helm release state"
capture_with_retry() {
  local destination="$1" attempt temporary
  shift
  temporary="${destination}.tmp"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if "$@" > "$temporary"; then
      mv "$temporary" "$destination"
      return 0
    fi
    sleep $((attempt * 2))
  done
  rm -f "$temporary"
  return 1
}
capture_with_retry "$STAGE_DIR/helm/releases.json" helm list --all-namespaces --output json \
  || fail "Helm release inventory failed after retries"
while IFS=$'\t' read -r namespace release revision; do
  [[ -n "$release" ]] || continue
  safe_name="${namespace}-${release}"
  capture_with_retry "$STAGE_DIR/helm/${safe_name}-values.yaml" helm get values "$release" -n "$namespace" --all \
    || fail "Helm values capture failed after retries: ${namespace}/${release}"
  capture_with_retry "$STAGE_DIR/helm/${safe_name}-manifest.yaml" helm get manifest "$release" -n "$namespace" \
    || fail "Helm manifest capture failed after retries: ${namespace}/${release}"
  capture_with_retry "$STAGE_DIR/helm/${safe_name}-hooks.yaml" helm get hooks "$release" -n "$namespace" \
    || fail "Helm hooks capture failed after retries: ${namespace}/${release}"
  printf '%s\t%s\t%s\n' "$namespace" "$release" "$revision" >> "$STAGE_DIR/helm/revisions.tsv"
done < <(jq -r '.[] | [.namespace,.name,(.revision|tostring)] | @tsv' "$STAGE_DIR/helm/releases.json")

log "capturing Kubernetes API resources"
run_with_retry kubectl api-resources --verbs=list --namespaced=true -o name \
  | sort -u > "$STAGE_DIR/cluster/namespaced-api-resources.txt" \
  || fail "namespaced Kubernetes API discovery failed after retries"
run_with_retry kubectl api-resources --verbs=list --namespaced=false -o name \
  | sort -u > "$STAGE_DIR/cluster/cluster-api-resources.txt" \
  || fail "cluster-scoped Kubernetes API discovery failed after retries"
kubectl_export() {
  local destination="$1" attempt temporary
  shift
  temporary="${destination}.tmp"
  for attempt in 1 2 3 4 5 6 7 8 9 10; do
    if kubectl --request-timeout=90s get "$@" -o yaml > "$temporary" 2>> "$STAGE_DIR/cluster/export-errors.log"; then
      mv "$temporary" "$destination"
      return 0
    fi
    sleep $((attempt * 2))
  done
  rm -f "$temporary"
  return 1
}
RESOURCE_FAILURES=0
while IFS= read -r resource; do
  case "$resource" in
    events|events.events.k8s.io|bindings|tokenreviews.authentication.k8s.io|subjectaccessreviews.authorization.k8s.io|selfsubjectaccessreviews.authorization.k8s.io|selfsubjectrulesreviews.authorization.k8s.io|localsubjectaccessreviews.authorization.k8s.io) continue ;;
  esac
  filename=${resource//\//_}
  if ! kubectl_export "$STAGE_DIR/cluster/resources/namespaced/${filename}.yaml" "$resource" --all-namespaces; then
    RESOURCE_FAILURES=$((RESOURCE_FAILURES + 1))
    printf 'namespaced/%s\n' "$resource" >> "$STAGE_DIR/cluster/export-failures.txt"
  fi
done < "$STAGE_DIR/cluster/namespaced-api-resources.txt"
while IFS= read -r resource; do
  case "$resource" in
    componentstatuses|tokenreviews.authentication.k8s.io|subjectaccessreviews.authorization.k8s.io|selfsubjectaccessreviews.authorization.k8s.io|selfsubjectrulesreviews.authorization.k8s.io) continue ;;
  esac
  filename=${resource//\//_}
  if ! kubectl_export "$STAGE_DIR/cluster/resources/cluster/${filename}.yaml" "$resource"; then
    RESOURCE_FAILURES=$((RESOURCE_FAILURES + 1))
    printf 'cluster/%s\n' "$resource" >> "$STAGE_DIR/cluster/export-failures.txt"
  fi
done < "$STAGE_DIR/cluster/cluster-api-resources.txt"
capture_with_retry "$STAGE_DIR/cluster/version.yaml" kubectl --request-timeout=90s version -o yaml \
  || fail "Kubernetes version capture failed after retries"
capture_with_retry "$STAGE_DIR/cluster/readyz.txt" kubectl --request-timeout=90s get --raw='/readyz?verbose' \
  || fail "Kubernetes readiness capture failed after retries"
if (( RESOURCE_FAILURES > 0 )) && [[ "$ALLOW_INCOMPLETE" != true ]]; then
  printf '[cluster-backup] failed API resources:\n' >&2
  sed 's/^/[cluster-backup]   /' "$STAGE_DIR/cluster/export-failures.txt" >&2
  fail "$RESOURCE_FAILURES Kubernetes API resource exports failed; see export-errors.log"
fi

build_ssh_args() {
  local quoted_identity quoted_known_hosts proxy_command
  [[ -n "$SSH_IDENTITY" && -f "$SSH_IDENTITY" ]] || fail "SSH identity is missing: ${SSH_IDENTITY:-not configured}"
  SSH_ARGS=(-i "$SSH_IDENTITY" -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new
    -o ConnectTimeout=30 -o ConnectionAttempts=3 -o ServerAliveInterval=15 -o ServerAliveCountMax=4)
  if [[ -n "$SSH_KNOWN_HOSTS_FILE" ]]; then
    mkdir -p "$(dirname "$SSH_KNOWN_HOSTS_FILE")"
    touch "$SSH_KNOWN_HOSTS_FILE"
    chmod 0600 "$SSH_KNOWN_HOSTS_FILE"
    SSH_ARGS+=(-o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}")
  fi
  if [[ -n "$SSH_BASTION" ]]; then
    printf -v quoted_identity '%q' "$SSH_IDENTITY"
    proxy_command="ssh -i ${quoted_identity} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new"
    if [[ -n "$SSH_KNOWN_HOSTS_FILE" ]]; then
      printf -v quoted_known_hosts '%q' "$SSH_KNOWN_HOSTS_FILE"
      proxy_command+=" -o UserKnownHostsFile=${quoted_known_hosts}"
    fi
    proxy_command+=" -o ConnectTimeout=20 -W %h:%p ${SSH_USER}@${SSH_BASTION}"
    SSH_ARGS+=(-o "ProxyCommand=${proxy_command}")
  fi
}

ssh_capture_with_retry() {
  local destination="$1" host="$2" remote_command="$3" attempt temporary
  temporary="${destination}.tmp"
  for attempt in 1 2 3; do
    rm -f "$temporary"
    if ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${host}" "$remote_command" > "$temporary" \
      && [[ -s "$temporary" ]]; then
      mv "$temporary" "$destination"
      return 0
    fi
    sleep $((attempt * 3))
  done
  rm -f "$temporary"
  return 1
}

ssh_run_with_retry() {
  local host="$1" remote_command="$2" attempt
  for attempt in 1 2 3; do
    if ssh -n "${SSH_ARGS[@]}" "${SSH_USER}@${host}" "$remote_command"; then
      return 0
    fi
    sleep $((attempt * 3))
  done
  return 1
}

if [[ "$SKIP_CONTROL_PLANE" != true ]]; then
  [[ -n "$SSH_BASTION" ]] || SSH_BASTION=$(yq -r '.bastion_public_ip // ""' "$INFRA_FACTS")
  [[ -n "$CONTROL_PLANE_HOST" ]] || CONTROL_PLANE_HOST=$(yq -r '.first_master_ip // ""' "$INFRA_FACTS")
  [[ -n "$CONTROL_PLANE_HOST" ]] || fail "first control-plane host is absent from infra facts"
  build_ssh_args
  log "capturing verified etcd snapshot from $CONTROL_PLANE_HOST"
  ssh_capture_with_retry "$STAGE_DIR/etcd/endpoint-health.txt" "$CONTROL_PLANE_HOST" \
    'set -eu; . /etc/etcd.env; export ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl endpoint health --cluster' \
    || fail "etcd endpoint health capture failed after retries"
  # shellcheck disable=SC2029
  ssh_capture_with_retry "$STAGE_DIR/etcd/snapshot-status.json" "$CONTROL_PLANE_HOST" \
    "set -eu; rm -f /tmp/${BACKUP_ID}.db /tmp/${BACKUP_ID}.db.part; . /etc/etcd.env; export ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl snapshot save /tmp/${BACKUP_ID}.db >/dev/null; if command -v etcdutl >/dev/null; then etcdutl --write-out=json snapshot status /tmp/${BACKUP_ID}.db; else etcdctl --write-out=json snapshot status /tmp/${BACKUP_ID}.db; fi" \
    || fail "etcd snapshot creation or verification failed after retries"
  # shellcheck disable=SC2029
  ssh_capture_with_retry "$STAGE_DIR/etcd/snapshot.db" "$CONTROL_PLANE_HOST" \
    "cat /tmp/${BACKUP_ID}.db" \
    || fail "etcd snapshot transfer failed after retries"
  [[ -s "$STAGE_DIR/etcd/snapshot.db" ]] || fail "etcd snapshot is empty"
  ssh_run_with_retry "$CONTROL_PLANE_HOST" "rm -f /tmp/${BACKUP_ID}.db /tmp/${BACKUP_ID}.db.part" \
    || fail "transferred etcd snapshot could not be removed from the control plane"
  ssh_capture_with_retry "$STAGE_DIR/etcd/members.json" "$CONTROL_PLANE_HOST" \
    'set -eu; . /etc/etcd.env; export ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl --write-out=json member list' \
    || fail "etcd membership capture failed after retries"

  log "capturing control-plane PKI and static configuration"
  while IFS=$'\t' read -r node host; do
    [[ -n "$host" ]] || continue
    ssh_capture_with_retry "$STAGE_DIR/control-plane/${node}.tar.gz" "$host" \
      'tar --numeric-owner -C / -czf - etc/kubernetes etc/ssl/etcd etc/etcd.env' \
      || fail "control-plane archive transfer failed after retries: $node"
    [[ -s "$STAGE_DIR/control-plane/${node}.tar.gz" ]] || fail "empty control-plane archive for $node"
  done < <(yq -r '.master_ips | to_entries | .[] | [.key,.value] | @tsv' "$INFRA_FACTS")
fi

if [[ "$SKIP_CLOUD" != true ]]; then
  command -v hcloud >/dev/null || fail "hcloud is required for cloud-state capture"
  [[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required for cloud-state capture"
  log "capturing Hetzner infrastructure state"
  hcloud_safe version > "$STAGE_DIR/cloud/hcloud-version.txt"
  hcloud_safe server list --selector "project=${PROJECT}" -o json > "$STAGE_DIR/cloud/servers.json"
  for spec in "network:${PROJECT}-network" "firewall:${PROJECT}-fw-bastion" "firewall:${PROJECT}-fw-nodes" \
    "load-balancer:${PROJECT}-lb" "placement-group:${PROJECT}-spread" "ssh-key:${PROJECT}-key" "zone:${DNS_ZONE}"; do
    kind=${spec%%:*}; name=${spec#*:}; safe_name=${name//[^[:alnum:]._-]/_}
    file="${kind//-/_}-${safe_name}"
    # macOS still ships Bash 3.2, where expanding an empty array under `set -u`
    # raises an unbound-variable error. Keep the optional argument branch
    # explicit so cloud capture is portable and cannot silently truncate a
    # recovery bundle before application-consistent backups run.
    if [[ "$kind" == load-balancer ]]; then
      if hcloud_safe "$kind" describe "$name" --expand-targets -o json \
        > "$STAGE_DIR/cloud/${file}.json" 2> "$STAGE_DIR/cloud/${file}.error"; then
        describe_rc=0
      else
        describe_rc=$?
      fi
    else
      if hcloud_safe "$kind" describe "$name" -o json \
        > "$STAGE_DIR/cloud/${file}.json" 2> "$STAGE_DIR/cloud/${file}.error"; then
        describe_rc=0
      else
        describe_rc=$?
      fi
    fi
    if (( describe_rc != 0 )); then
      if grep -qi 'not found' "$STAGE_DIR/cloud/${file}.error"; then
        printf '{"state":"absent","name":"%s"}\n' "$name" > "$STAGE_DIR/cloud/${file}.json"
      else
        sed 's/^/[cluster-backup]   /' "$STAGE_DIR/cloud/${file}.error" >&2
        fail "Hetzner cloud-state capture failed for ${kind}/${name}; refusing to record a transient API failure as resource absence"
      fi
    fi
  done
  dns_zone_state_file="$STAGE_DIR/cloud/zone-${DNS_ZONE//[^[:alnum:]._-]/_}.json"
  if [[ -n "$DNS_ZONE" ]] && jq -e '(.state // "present") != "absent"' "$dns_zone_state_file" >/dev/null; then
    hcloud_safe zone rrset list "$DNS_ZONE" -o json \
      | jq --arg root "$DNS_RECORD_ROOT" '[.[] | select(
          .name == $root
          or .name == (if $root == "@" then "*" else "*." + $root end)
          or .name == (if $root == "@" then "vpn" else "vpn." + $root end)
        )]' > "$STAGE_DIR/cloud/zone-rrsets.json"
  fi
  volume_ids=$(kubectl get pv -o json | jq '[.items[]
    | select(.spec.csi.driver=="csi.hetzner.cloud")
    | .spec.csi.volumeHandle | tonumber]')
  hcloud_safe volume list -o json \
    | jq --argjson ids "$volume_ids" -c '.[] | select(.id as $id | $ids | index($id))' \
    > "$STAGE_DIR/cloud/volumes.jsonl"
fi

APP_BACKUP_RESULT=skipped
NATIVE_CATALOG_SHA256=""
if [[ "$RUN_APP_BACKUPS" == true ]]; then
  log "triggering application-consistent backups"
  PROJECT_NAME="$PROJECT" BACKUP_RUN_ID="$BACKUP_ID" BACKUP_ALLOW_VELERO_VAULT_FALLBACK=true \
    "$SCRIPT_DIR/backup-all.sh" --config "$CONFIG_FILE" \
    --result-json "$STAGE_DIR/application-backups/native-backups.json" --force \
    | tee "$STAGE_DIR/application-backups/backup-all.log"
  jq -e --arg project "$PROJECT" --arg backupId "$BACKUP_ID" '
    .schema_version == 2 and .project == $project and .backup_id == $backupId and
    .completeness == "complete" and .summary.failed == 0 and
    .summary.expected == (.artifacts | length) and (.artifacts | type == "array") and
    all(.artifacts[]; .state == "completed" or .state == "velero-fallback" or .state == "disabled")
  ' "$STAGE_DIR/application-backups/native-backups.json" >/dev/null \
    || fail "structured native backup catalog is missing, incomplete, or belongs to another project"
  NATIVE_CATALOG_SHA256=$(sha256_file "$STAGE_DIR/application-backups/native-backups.json" | awk '{print $1}')
  APP_BACKUP_RESULT=completed
fi

# Evaluate claims after application-consistent backups and immediately before
# the Velero filesystem snapshot so the evidence describes the protected set.
evaluate_pvc_protection_gate

VELERO_BACKUP_NAME=""
VELERO_BACKUP_RESULT=skipped
if [[ "$RUN_VELERO_BACKUP" == true ]]; then
  log "triggering external Velero resource and PVC backup"
  kubectl wait backupstoragelocation/default -n velero --for=jsonpath='{.status.phase}'=Available --timeout=300s
  PODS_SNAPSHOT="${WORK_DIR}/pods.json"
  kubectl get pods --all-namespaces -o json > "$PODS_SNAPSHOT"
  jq -r '
    .items[] as $pod
    | select($pod.status.phase == "Running")
    | $pod.spec.volumes[]?
    | select(.persistentVolumeClaim != null)
    | [$pod.metadata.namespace, $pod.metadata.name, .name]
    | @tsv' "$PODS_SNAPSHOT" | sort -u > "$STAGE_DIR/application-backups/mounted-pod-volumes.expected.tsv"
  jq -r '
    .items[] as $pod
    | select($pod.status.phase == "Running")
    | [$pod.spec.volumes[]? | select(.persistentVolumeClaim != null) | .name] as $volumes
    | select($volumes | length > 0)
    | [$pod.metadata.namespace, $pod.metadata.name,
       ($pod.metadata.annotations["backup.velero.io/backup-volumes"] // "__ABSENT__"),
       ($volumes | unique | sort | join(","))]
    | @tsv' "$PODS_SNAPSHOT" | sort -u > "$POD_ANNOTATIONS_FILE"
  while IFS=$'\t' read -r namespace pod _previous volumes; do
    kubectl annotate pod -n "$namespace" "$pod" \
      "backup.velero.io/backup-volumes=${volumes}" --overwrite >/dev/null
  done < "$POD_ANNOTATIONS_FILE"
  VELERO_BACKUP_NAME=$(printf '%s' "$BACKUP_ID" | tr '[:upper:]' '[:lower:]')
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
  defaultVolumesToFsBackup: false
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
    failed_volume_backups=$(kubectl get podvolumebackups -n velero \
      -l "velero.io/backup-name=${VELERO_BACKUP_NAME}" -o json 2>/dev/null \
      | jq '[.items[] | select((.status.phase // "") == "Failed")] | length' \
      || echo 0)
    if (( failed_volume_backups > 0 )); then
      kubectl get podvolumebackups -n velero \
        -l "velero.io/backup-name=${VELERO_BACKUP_NAME}" -o json \
        > "$STAGE_DIR/application-backups/pod-volume-backups.json"
      fail "$failed_volume_backups Velero filesystem backup(s) failed before the backup completed"
    fi
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
  restore_backup_annotations
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
  --arg bastionType "$BASTION_SERVER_TYPE" --arg controlPlaneType "$CONTROL_PLANE_SERVER_TYPE" \
  --arg workerType "$WORKER_SERVER_TYPE" \
  --arg sourceClusterUid "$SOURCE_CLUSTER_UID" \
  --arg completeness "$COMPLETENESS" --arg app "$APP_BACKUP_RESULT" \
  --arg velero "$VELERO_BACKUP_RESULT" --arg veleroName "$VELERO_BACKUP_NAME" \
  --arg pvcGate "$PVC_GATE_RESULT" --argjson pvcGateFailures "$PVC_GATE_FAILURES" \
  --argjson resourceFailures "$RESOURCE_FAILURES" --argjson hasControlPlane "$HAS_CONTROL_PLANE" \
  --argjson hasCloud "$HAS_CLOUD" --argjson vaultInitRequired "$VAULT_INIT_REQUIRED" \
  --argjson hasVaultInit "$VAULT_INIT_INCLUDED" \
  --arg repositoryBundleSha "$REPOSITORY_BUNDLE_SHA256" \
  --arg worktreePatchSha "$WORKTREE_PATCH_SHA256" \
  --arg gitRevision "$GIT_REVISION" --arg gitRevisionSha "$GIT_REVISION_SHA256" \
  --arg untrackedArchiveSha "$UNTRACKED_ARCHIVE_SHA256" \
  --arg untrackedPathsSha "$UNTRACKED_PATHS_SHA256" \
  --arg nativeCatalogSha "$NATIVE_CATALOG_SHA256" \
  --argjson untrackedFileCount "$UNTRACKED_FILE_COUNT" \
  --arg veleroPrefix "$VELERO_DR_PREFIX" \
  '{schema_version:2,backup_id:$id,created_at:$timestamp,project:$project,domain:$domain,
    profile:$profile,source_context:$context,source_cluster_uid:$sourceClusterUid,
    provider_machine_types:{bastion:$bastionType,control_plane:$controlPlaneType,
      worker:$workerType},
    completeness:$completeness,
    application_backups:$app,velero_backup:$velero,velero_backup_name:$veleroName,
    velero_storage_prefix:$veleroPrefix,
    native_backup_catalog:{included:($app == "completed"),
      bundle_path:(if $app == "completed" then "application-backups/native-backups.json" else null end),
      sha256:(if $app == "completed" then $nativeCatalogSha else null end)},
    pvc_protection_gate:{status:$pvcGate,failures:$pvcGateFailures,
      evidence:"application-backups/pvc-protection-evidence.json"},
    kubernetes_resource_export_failures:$resourceFailures,
    contains:{platform_config:true,generated_secrets:true,kubespray_inventory:true,
      helm_state:true,kubernetes_api_exports:true,etcd_snapshot:$hasControlPlane,
      control_plane_pki:$hasControlPlane,cloud_state:$hasCloud,
      vault_init_material:$hasVaultInit},
    repository_state:{bundle_path:"config/repository.bundle",
      revision_path:"config/git-revision.txt",revision:$gitRevision,
      tracked_patch_path:"config/worktree.patch",
      tracked_patch_scope:"HEAD-to-working-tree-including-index",
      untracked_archive_path:"config/repository-untracked.tar",
      untracked_paths_path:"config/repository-untracked-files.txt",
      untracked_file_count:$untrackedFileCount,
      sha256:{bundle:$repositoryBundleSha,revision:$gitRevisionSha,tracked_patch:$worktreePatchSha,
        untracked_archive:$untrackedArchiveSha,untracked_paths:$untrackedPathsSha}},
    recovery_dependencies:{vault_init:{required:$vaultInitRequired,
      included:$hasVaultInit,
      encryption:(if $hasVaultInit then "ansible-vault" else null end),
      bundle_path:(if $hasVaultInit then "config/vault-init.json.vault" else null end)}},
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
ARCHIVE_SHA256=$(sha256_file "$FINAL_ARCHIVE" | awk '{print $1}')
printf '%s  %s\n' "$ARCHIVE_SHA256" "$(basename "$FINAL_ARCHIVE")" \
  > "${FINAL_ARCHIVE}.sha256"
chmod 600 "${FINAL_ARCHIVE}.sha256"

REMOTE_PUBLISH_RESULT=skipped
REMOTE_ARCHIVE_KEY=""
REMOTE_CHECKSUM_KEY=""
REMOTE_RECEIPT_KEY=""
REMOTE_VERIFIED=false
RECEIPT_CREATED_AT=$(date -u +%Y-%m-%dT%H:%M:%SZ)
RECEIPT_PATH="${FINAL_ARCHIVE}.manifest.json"
write_completion_receipt() {
  local target="$1" remote_published="$2" receipt_uploaded_last="$3"
  local publication_state="$4"
  jq -n --arg backupId "$BACKUP_ID" --arg archive "$(basename "$FINAL_ARCHIVE")" \
    --arg encryption "$ENCRYPTION" --arg completeness "$COMPLETENESS" \
    --arg veleroBackup "$VELERO_BACKUP_NAME" --arg sha256 "$ARCHIVE_SHA256" \
    --arg createdAt "$RECEIPT_CREATED_AT" --arg endpoint "$DR_ENDPOINT" \
    --arg bucket "$DR_BUCKET" --arg archiveKey "$REMOTE_ARCHIVE_KEY" \
    --arg checksumKey "$REMOTE_CHECKSUM_KEY" --arg receiptKey "$REMOTE_RECEIPT_KEY" \
    --arg publicationState "$publication_state" \
    --argjson remotePublished "$remote_published" --argjson remoteVerified "$REMOTE_VERIFIED" \
    --argjson receiptUploadedLast "$receipt_uploaded_last" \
    --arg project "$PROJECT" --arg domain "$DOMAIN" --arg profile "$PROFILE" \
    --arg sourceContext "$CONTEXT" --arg sourceClusterUid "$SOURCE_CLUSTER_UID" \
    --arg veleroPrefix "$VELERO_DR_PREFIX" \
    --arg nativeCatalogSha "$NATIVE_CATALOG_SHA256" \
    '{schema_version:2,receipt_type:"encrypted-cluster-backup",backup_id:$backupId,
      created_at:$createdAt,project:$project,domain:$domain,profile:$profile,
      source_context:$sourceContext,source_cluster_uid:$sourceClusterUid,
      velero_storage_prefix:$veleroPrefix,
      native_backup_catalog_sha256:$nativeCatalogSha,
      archive:$archive,encryption:$encryption,
      completeness:$completeness,velero_backup_name:$veleroBackup,sha256:$sha256,
      remote:{published:$remotePublished,download_sha256_verified:$remoteVerified,
        publication_state:$publicationState,
        endpoint:$endpoint,bucket:$bucket,archive_key:$archiveKey,
        checksum_key:$checksumKey,receipt_key:$receiptKey,
        publication_order:["archive","checksum","download-verify","receipt"],
        receipt_uploaded_last:$receiptUploadedLast}}' > "$target"
  chmod 600 "$target"
}

if [[ "$RUN_REMOTE_PUBLISH" == true ]]; then
  REMOTE_BASE_KEY="${DR_PREFIX}/${BACKUP_ID}"
  REMOTE_ARCHIVE_KEY="${REMOTE_BASE_KEY}/$(basename "$FINAL_ARCHIVE")"
  REMOTE_CHECKSUM_KEY="${REMOTE_ARCHIVE_KEY}.sha256"
  REMOTE_RECEIPT_KEY="${REMOTE_ARCHIVE_KEY}.manifest.json"
  # Persist a truthful local failure-state receipt before the first remote
  # write. It must not claim publication until the final receipt itself has
  # uploaded and been downloaded again successfully.
  write_completion_receipt "$RECEIPT_PATH" false false pending_archive
  log "publishing encrypted recovery archive to external DR storage"
  run_with_retry aws_dr s3 cp "$FINAL_ARCHIVE" \
    "s3://${DR_BUCKET}/${REMOTE_ARCHIVE_KEY}" --only-show-errors \
    || fail "encrypted recovery archive upload failed after retries"
  run_with_retry aws_dr s3 cp "${FINAL_ARCHIVE}.sha256" \
    "s3://${DR_BUCKET}/${REMOTE_CHECKSUM_KEY}" --only-show-errors \
    || fail "encrypted recovery checksum upload failed after retries"

  REMOTE_VERIFY_ARCHIVE="${WORK_DIR}/remote-$(basename "$FINAL_ARCHIVE")"
  run_with_retry aws_dr s3 cp "s3://${DR_BUCKET}/${REMOTE_ARCHIVE_KEY}" \
    "$REMOTE_VERIFY_ARCHIVE" --only-show-errors \
    || fail "encrypted recovery archive download failed after retries"
  REMOTE_ARCHIVE_SHA256=$(sha256_file "$REMOTE_VERIFY_ARCHIVE" | awk '{print $1}')
  [[ "$REMOTE_ARCHIVE_SHA256" == "$ARCHIVE_SHA256" ]] \
    || fail "downloaded remote recovery archive failed SHA-256 verification"
  REMOTE_VERIFIED=true
  write_completion_receipt "$RECEIPT_PATH" false false pending_receipt
  # The receipt is intentionally the final write. Consumers must treat an
  # archive without this object as an interrupted/incomplete publication.
  FINAL_RECEIPT="${WORK_DIR}/completed-receipt.json"
  write_completion_receipt "$FINAL_RECEIPT" true true complete
  run_with_retry aws_dr s3 cp "$FINAL_RECEIPT" \
    "s3://${DR_BUCKET}/${REMOTE_RECEIPT_KEY}" --only-show-errors \
    || fail "remote completion receipt upload failed after retries"
  REMOTE_RECEIPT_VERIFY="${WORK_DIR}/remote-receipt.json"
  run_with_retry aws_dr s3 cp "s3://${DR_BUCKET}/${REMOTE_RECEIPT_KEY}" \
    "$REMOTE_RECEIPT_VERIFY" --only-show-errors \
    || fail "remote completion receipt download failed after retries"
  cmp -s "$FINAL_RECEIPT" "$REMOTE_RECEIPT_VERIFY" \
    || fail "remote completion receipt differs from the local manifest"
  mv "$FINAL_RECEIPT" "$RECEIPT_PATH" \
    || fail "local completion receipt finalization failed"
  chmod 600 "$RECEIPT_PATH"
  REMOTE_PUBLISH_RESULT=completed
else
  write_completion_receipt "$RECEIPT_PATH" false false not_requested
fi

log "backup complete: $FINAL_ARCHIVE"
log "checksum: ${FINAL_ARCHIVE}.sha256"
log "remote publication: $REMOTE_PUBLISH_RESULT"
if [[ "$REMOTE_PUBLISH_RESULT" == completed ]]; then
  log "remote receipt: s3://${DR_BUCKET}/${REMOTE_RECEIPT_KEY}"
fi
log "Velero backup: ${VELERO_BACKUP_NAME:-not-requested}"
