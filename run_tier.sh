#!/usr/bin/env bash

# Deploy one exact named profile from an isolated controller workspace.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROFILES_DIR="${SCRIPT_DIR}/platform-orchestrator/profiles"
SUPPORTED_PROFILES="minimal small medium medium-optimized production"

usage() {
  cat <<'EOF'
Usage: ./run_tier.sh <profile> [options]

Profiles:
  minimal | small | medium | medium-optimized | production

Options:
  --campaign-id ID       Stable campaign identifier (default: manual)
  --project NAME         Unique Hetzner project prefix
  --domain DOMAIN        Cluster base domain
  --email EMAIL          ACME/operator email
  --home DIR             Isolated controller HOME
  --run-root DIR         Isolated state directory
  --config FILE          Generated runtime profile path
  --log-file FILE        Deployment log path
  --api-port PORT        Unique local Kubernetes API tunnel port
  --ssh-key FILE         Controller private key path (before isolated HOME)
  --dr-endpoint URL      External S3-compatible DR endpoint
  --dr-bucket NAME       External DR bucket
  --dr-prefix PREFIX     Unique bucket prefix (default: PROJECT/velero)
  --dns-zone DOMAIN      Authoritative Hetzner parent zone
  --certificate-issuer   ClusterIssuer for test certificates
  --bastion-type TYPE    Capacity-equivalent bastion server type override
  --cp-type TYPE         Capacity-equivalent control-plane type override
  --worker-type TYPE     Capacity-equivalent worker server type override
  --manage-dns           Permit the infrastructure role to manage DNS
  --minimum-storage      Set PVC requests to 10Gi; use rebuildable SeaweedFS indexes
  --skip-kubespray       Resume after a verified successful Kubespray run
  --controller-forks N   Ansible worker forks for this controller (default: 2)
  --operator-state-root  Persistent encrypted operator state directory
  --dry-run              Generate and validate inputs; print, do not deploy
  -h, --help             Show this help

Secrets remain environment-only. HCLOUD_TOKEN, BACKUP_DR_ACCESS_KEY,
BACKUP_DR_SECRET_KEY, and ANSIBLE_VAULT_PASSWORD_FILE are loaded from the
repository .env when they are not already exported.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
contains_profile() {
  case " ${SUPPORTED_PROFILES} " in *" $1 "*) return 0 ;; *) return 1 ;; esac
}
require_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 requires a value"; }

PROFILE="${1:-}"
if [[ "$PROFILE" == "-h" || "$PROFILE" == "--help" || -z "$PROFILE" ]]; then
  usage
  [[ -n "$PROFILE" ]] && exit 0 || exit 2
fi
contains_profile "$PROFILE" || die "unsupported profile '$PROFILE'"
shift

CAMPAIGN_ID="${CAMPAIGN_ID:-manual}"
PROJECT="${PROJECT:-}"
DOMAIN="${DOMAIN:-}"
EMAIL="${EMAIL:-admin@n0xeid.xyz}"
RUN_ROOT="${RUN_ROOT:-}"
CONTROLLER_HOME="${CONTROLLER_HOME:-}"
CONFIG_FILE="${CONFIG_FILE:-}"
LOG_FILE="${LOG_FILE:-}"
API_PORT="${KUBE_API_LOCAL_PORT:-16443}"
SSH_KEY_PATH="${SSH_KEY_PATH:-${HOME}/.ssh/id_ed25519}"
CONTROLLER_COLLECTIONS_PATH="${ANSIBLE_COLLECTIONS_PATH:-${HOME}/.ansible/collections:/usr/share/ansible/collections}"
DR_ENDPOINT="${BACKUP_DR_ENDPOINT:-}"
DR_BUCKET="${BACKUP_DR_BUCKET:-}"
DR_PREFIX="${BACKUP_DR_PREFIX:-}"
DNS_ZONE="${HETZNER_DNS_ZONE:-}"
CERTIFICATE_ISSUER="${CERT_MANAGER_CLUSTER_ISSUER:-letsencrypt-staging}"
BASTION_TYPE=""
CP_TYPE=""
WORKER_TYPE=""
MANAGE_DNS=false
MINIMUM_STORAGE=false
SKIP_KUBESPRAY=false
CONTROLLER_FORKS="${CONTROLLER_FORKS:-2}"
OPERATOR_STATE_ROOT="${OPERATOR_STATE_ROOT:-}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --campaign-id) require_value "$1" "${2:-}"; CAMPAIGN_ID="$2"; shift 2 ;;
    --project) require_value "$1" "${2:-}"; PROJECT="$2"; shift 2 ;;
    --domain) require_value "$1" "${2:-}"; DOMAIN="$2"; shift 2 ;;
    --email) require_value "$1" "${2:-}"; EMAIL="$2"; shift 2 ;;
    --home) require_value "$1" "${2:-}"; CONTROLLER_HOME="$2"; shift 2 ;;
    --run-root) require_value "$1" "${2:-}"; RUN_ROOT="$2"; shift 2 ;;
    --config) require_value "$1" "${2:-}"; CONFIG_FILE="$2"; shift 2 ;;
    --log-file) require_value "$1" "${2:-}"; LOG_FILE="$2"; shift 2 ;;
    --api-port) require_value "$1" "${2:-}"; API_PORT="$2"; shift 2 ;;
    --ssh-key) require_value "$1" "${2:-}"; SSH_KEY_PATH="$2"; shift 2 ;;
    --dr-endpoint) require_value "$1" "${2:-}"; DR_ENDPOINT="$2"; shift 2 ;;
    --dr-bucket) require_value "$1" "${2:-}"; DR_BUCKET="$2"; shift 2 ;;
    --dr-prefix) require_value "$1" "${2:-}"; DR_PREFIX="$2"; shift 2 ;;
    --dns-zone) require_value "$1" "${2:-}"; DNS_ZONE="$2"; shift 2 ;;
    --certificate-issuer) require_value "$1" "${2:-}"; CERTIFICATE_ISSUER="$2"; shift 2 ;;
    --bastion-type) require_value "$1" "${2:-}"; BASTION_TYPE="$2"; shift 2 ;;
    --cp-type) require_value "$1" "${2:-}"; CP_TYPE="$2"; shift 2 ;;
    --worker-type) require_value "$1" "${2:-}"; WORKER_TYPE="$2"; shift 2 ;;
    --manage-dns) MANAGE_DNS=true; shift ;;
    --minimum-storage) MINIMUM_STORAGE=true; shift ;;
    --skip-kubespray) SKIP_KUBESPRAY=true; shift ;;
    --controller-forks) require_value "$1" "${2:-}"; CONTROLLER_FORKS="$2"; shift 2 ;;
    --operator-state-root) require_value "$1" "${2:-}"; OPERATOR_STATE_ROOT="$2"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option '$1'" ;;
  esac
done

[[ "$CAMPAIGN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid campaign id '$CAMPAIGN_ID'"
PROJECT="${PROJECT:-t5-${CAMPAIGN_ID}-${PROFILE}}"
DOMAIN="${DOMAIN:-${PROFILE}.${BASE_DOMAIN:-n0xeid.xyz}}"
DNS_ZONE="${DNS_ZONE:-${DOMAIN}}"
RUN_ROOT="${RUN_ROOT:-/private/tmp/ansible-k8s-${CAMPAIGN_ID}/${PROFILE}}"
CONTROLLER_HOME="${CONTROLLER_HOME:-${RUN_ROOT}/home}"
CONFIG_FILE="${CONFIG_FILE:-${RUN_ROOT}/platform.yaml}"
LOG_FILE="${LOG_FILE:-${RUN_ROOT}/logs/deploy.log}"
DR_PREFIX="${DR_PREFIX:-${PROJECT}/velero}"
PROFILE_FILE="${PROFILES_DIR}/${PROFILE}.yaml"
STATUS_FILE="${RUN_ROOT}/status.json"

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9-]{1,47}$ ]] || die "project must be 2-48 lowercase letters, numbers, or hyphens"
[[ "$DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$DOMAIN" == *.* ]] || die "invalid domain '$DOMAIN'"
[[ "$DNS_ZONE" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$DNS_ZONE" == *.* ]] || die "invalid DNS zone '$DNS_ZONE'"
[[ "$DOMAIN" == "$DNS_ZONE" || "$DOMAIN" == *."$DNS_ZONE" ]] \
  || die "domain '$DOMAIN' is not within DNS zone '$DNS_ZONE'"
[[ "$CERTIFICATE_ISSUER" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "invalid certificate issuer"
for server_type in "$BASTION_TYPE" "$CP_TYPE" "$WORKER_TYPE"; do
  [[ -z "$server_type" || "$server_type" =~ ^[a-z0-9][a-z0-9-]{0,31}$ ]] \
    || die "invalid Hetzner server type '$server_type'"
done
[[ "$EMAIL" == *@*.* ]] || die "invalid email '$EMAIL'"
if [[ ! "$API_PORT" =~ ^[0-9]+$ ]] || ((API_PORT < 1024 || API_PORT > 65535)); then
  die "invalid API port '$API_PORT'"
fi
if [[ ! "$CONTROLLER_FORKS" =~ ^[0-9]+$ ]] || ((CONTROLLER_FORKS < 1 || CONTROLLER_FORKS > 20)); then
  die "controller forks must be between 1 and 20"
fi
[[ "$SSH_KEY_PATH" == /* ]] || die "SSH key path must be absolute"
[[ -f "$PROFILE_FILE" ]] || die "profile source is missing: $PROFILE_FILE"
command -v yq >/dev/null 2>&1 || die "yq v4 is required"
command -v jq >/dev/null 2>&1 || die "jq is required"

mkdir -p "$RUN_ROOT" "$CONTROLLER_HOME/.kube" "$(dirname "$CONFIG_FILE")" "$(dirname "$LOG_FILE")"
CONTROL_PATH_HASH="$(printf '%s' "${PROJECT}:${API_PORT}" | shasum -a 256 | cut -c1-16)"
SHORT_CONTROL_PATH_DIR="/tmp/ansible-k8s-cp/${CONTROL_PATH_HASH}"
mkdir -p "$RUN_ROOT/tmp" "$RUN_ROOT/ansible-facts" "$RUN_ROOT/ansible-local" "$SHORT_CONTROL_PATH_DIR"
RUN_ROOT="$(cd "$RUN_ROOT" && pwd)"
CONTROLLER_HOME="$(cd "$CONTROLLER_HOME" && pwd)"
CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
LOG_FILE="$(cd "$(dirname "$LOG_FILE")" && pwd)/$(basename "$LOG_FILE")"
STATUS_FILE="${RUN_ROOT}/status.json"
chmod 700 "$RUN_ROOT" "$CONTROLLER_HOME" "$CONTROLLER_HOME/.kube" "$RUN_ROOT/tmp" \
  "$RUN_ROOT/ansible-facts" "$RUN_ROOT/ansible-local" "$SHORT_CONTROL_PATH_DIR"

export HOME="$CONTROLLER_HOME"
export TMPDIR="$RUN_ROOT/tmp"
export KUBECONFIG="$CONTROLLER_HOME/.kube/config"
export ANSIBLE_CACHE_PLUGIN_CONNECTION="$RUN_ROOT/ansible-facts"
export ANSIBLE_LOCAL_TEMP="$RUN_ROOT/ansible-local"
export ANSIBLE_REMOTE_TEMP="/tmp/.ansible-${PROJECT}"
export ANSIBLE_SSH_CONTROL_PATH_DIR="$SHORT_CONTROL_PATH_DIR"
export ANSIBLE_COLLECTIONS_PATH="$CONTROLLER_COLLECTIONS_PATH"
export ANSIBLE_FORKS="$CONTROLLER_FORKS"
export ANSIBLE_PIPELINING=true
export HELM_CACHE_HOME="$CONTROLLER_HOME/.cache/helm"
export HELM_CONFIG_HOME="$CONTROLLER_HOME/.config/helm"
export HELM_DATA_HOME="$CONTROLLER_HOME/.local/share/helm"
export KUBE_API_LOCAL_PORT="$API_PORT"
export CONTROLLER_TMP_DIR="$RUN_ROOT/tmp/controller"
mkdir -p "$HELM_CACHE_HOME" "$HELM_CONFIG_HOME" "$HELM_DATA_HOME" "$CONTROLLER_TMP_DIR"

# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/scripts/load-project-env.sh"
OPERATOR_STATE_ROOT="${OPERATOR_STATE_ROOT:-${SCRIPT_DIR}/.campaign-state/${PROJECT}}"
[[ "$OPERATOR_STATE_ROOT" == /* ]] || die "operator state root must be absolute"
mkdir -p "$OPERATOR_STATE_ROOT"
chmod 700 "$OPERATOR_STATE_ROOT"
DR_ENDPOINT="${DR_ENDPOINT:-${BACKUP_DR_ENDPOINT:-}}"
DR_BUCKET="${DR_BUCKET:-${BACKUP_DR_BUCKET:-}}"
DR_ACCESS_KEY="${BACKUP_DR_ACCESS_KEY:-}"
DR_SECRET_KEY="${BACKUP_DR_SECRET_KEY:-}"
if [[ -x "${SCRIPT_DIR}/.venv/bin/ansible-playbook" ]]; then
  PATH="${SCRIPT_DIR}/.venv/bin:${PATH}"
  export PATH
fi

cp "$PROFILE_FILE" "$CONFIG_FILE"
PROJECT="$PROJECT" DOMAIN="$DOMAIN" EMAIL="$EMAIL" CAMPAIGN_ID="$CAMPAIGN_ID" \
DR_ENDPOINT="$DR_ENDPOINT" DR_BUCKET="$DR_BUCKET" DR_PREFIX="$DR_PREFIX" \
yq -i '
  .global.project = strenv(PROJECT) |
  .global.domain = strenv(DOMAIN) |
  .global.email = strenv(EMAIL) |
  .global.campaign_id = strenv(CAMPAIGN_ID) |
  .backup.disaster_recovery.endpoint = strenv(DR_ENDPOINT) |
  .backup.disaster_recovery.bucket = strenv(DR_BUCKET) |
  .backup.disaster_recovery.prefix = strenv(DR_PREFIX)
' "$CONFIG_FILE"

if $MINIMUM_STORAGE; then
  yq -i '
    .storage.size = "10Gi" |
    .storage.master_size = "10Gi" |
    .storage.size_per_replica = "10Gi" |
    .storage.index_size = "10Gi" |
    .storage.index_persistent = false |
    .storage.filer_size = "10Gi" |
    .secrets.vault.storage_size = "10Gi" |
    .databases.postgresql.storage_size = "10Gi" |
    .databases.mongodb.storage_size = "10Gi" |
    .gitlab.gitaly_storage_size = "10Gi" |
    .gitlab.backup_persistence_size = "10Gi" |
    .observability.metrics.storage_size = "10Gi" |
    .observability.pmm.storage_size = "10Gi" |
    .elasticsearch.master.storage_size = "10Gi" |
    .elasticsearch.data.storage_size = "10Gi" |
    .dragonfly.snapshot_storage = "10Gi" |
    .tracing.storage_size = "10Gi" |
    .postal.mariadb_storage = "10Gi" |
    .coroot.storage_size = "10Gi" |
    .coroot.clickhouse.storage_size = "10Gi" |
    .campaign.minimum_storage = true
  ' "$CONFIG_FILE"
fi

[[ "$(yq -r '.platform_profile' "$CONFIG_FILE")" == "$PROFILE" ]] || die "runtime config lost profile identity"
case "$PROFILE" in
  medium-optimized|production)
    expected_tier_resource="medium:small"
    [[ "$PROFILE" == production ]] && expected_tier_resource="production:small"
    [[ "$(yq -r '.tier + ":" + .resource_tier' "$CONFIG_FILE")" == "$expected_tier_resource" ]] \
      || die "$PROFILE must remain tier/resource_tier=${expected_tier_resource/:/\/}"
    ;;
  *)
    [[ "$(yq -r '.tier + ":" + .resource_tier' "$CONFIG_FILE")" == "${PROFILE}:${PROFILE}" ]] \
      || die "$PROFILE tier/resource-tier contract changed"
    ;;
esac

if [[ "$(yq -r '(.backup.enabled and .backup.disaster_recovery.enabled) // false' "$CONFIG_FILE")" == true ]]; then
  [[ -n "$DR_ENDPOINT" && "$DR_ENDPOINT" != *'.svc'* && "$DR_ENDPOINT" != *seaweedfs* ]] \
    || die "$PROFILE requires an independent external DR endpoint"
  [[ ${#DR_BUCKET} -gt 2 ]] || die "$PROFILE requires an external DR bucket"
  [[ ${#DR_ACCESS_KEY} -ge 8 ]] || die "BACKUP_DR_ACCESS_KEY must contain at least 8 characters"
  [[ ${#DR_SECRET_KEY} -ge 16 ]] || die "BACKUP_DR_SECRET_KEY must contain at least 16 characters"
fi

write_status() {
  local state="$1" rc="$2"
  jq -n \
    --arg campaign "$CAMPAIGN_ID" --arg profile "$PROFILE" --arg project "$PROJECT" \
    --arg domain "$DOMAIN" --arg state "$state" --arg config "$CONFIG_FILE" \
    --arg log "$LOG_FILE" --argjson api_port "$API_PORT" --argjson rc "$rc" \
    '{campaign_id:$campaign,profile:$profile,project:$project,domain:$domain,state:$state,
      api_port:$api_port,config:$config,log:$log,exit_code:$rc,
      updated_at:(now|todateiso8601)}' >"${STATUS_FILE}.tmp"
  mv "${STATUS_FILE}.tmp" "$STATUS_FILE"
}

ANSIBLE_PLAYBOOK="$(command -v ansible-playbook || true)"
DEPLOY_ARGS=(
  playbooks/deploy_platform.yml
  -e "@${CONFIG_FILE}"
  -e "project_name=${PROJECT}"
  -e "domain=${DOMAIN}"
  -e "email=${EMAIL}"
  -e "hetzner_manage_dns=${MANAGE_DNS}"
  -e "hetzner_dns_zone=${DNS_ZONE}"
  -e "cert_manager_cluster_issuer=${CERTIFICATE_ISSUER}"
  -e "k8s_api_local_port=${API_PORT}"
  -e "ssh_key_path=${SSH_KEY_PATH}"
  -e "controller_tmp_dir=${CONTROLLER_TMP_DIR}"
  -e "backup_dr_storage_endpoint=${DR_ENDPOINT}"
  -e "backup_dr_storage_bucket=${DR_BUCKET}"
  -e "backup_dr_storage_prefix=${DR_PREFIX}"
  -e "secrets_file=${OPERATOR_STATE_ROOT}/.platform-secrets.yml"
  -e "vault_init_output_file=${OPERATOR_STATE_ROOT}/.vault-init-${PROJECT}.json"
)
[[ -z "$BASTION_TYPE" ]] || DEPLOY_ARGS+=(-e "hetzner_bastion_type=${BASTION_TYPE}")
[[ -z "$CP_TYPE" ]] || DEPLOY_ARGS+=(-e "hetzner_cp_type=${CP_TYPE}")
[[ -z "$WORKER_TYPE" ]] || DEPLOY_ARGS+=(-e "hetzner_worker_type=${WORKER_TYPE}")
$SKIP_KUBESPRAY && DEPLOY_ARGS+=(-e "skip_kubespray=true")

printf 'Campaign: %s\nProfile: %s\nProject: %s\nDomain: %s\nAPI port: %s\nConfig: %s\nLog: %s\n' \
  "$CAMPAIGN_ID" "$PROFILE" "$PROJECT" "$DOMAIN" "$API_PORT" "$CONFIG_FILE" "$LOG_FILE"

if $DRY_RUN; then
  write_status planned 0
  printf 'DRY-RUN: '
  printf '%q ' "${ANSIBLE_PLAYBOOK:-ansible-playbook}" "${DEPLOY_ARGS[@]}"
  printf '\n'
  exit 0
fi

[[ -n "$ANSIBLE_PLAYBOOK" ]] || die "ansible-playbook is required"
: "${HCLOUD_TOKEN:?Set HCLOUD_TOKEN directly or in ${SCRIPT_DIR}/.env}"
export HCLOUD_TOKEN
cd "$SCRIPT_DIR"

write_status validating 0
"$ANSIBLE_PLAYBOOK" playbooks/validate_profile.yml -e "@${CONFIG_FILE}" 2>&1 | tee -a "$LOG_FILE"
write_status running 0

set +e
"$ANSIBLE_PLAYBOOK" "${DEPLOY_ARGS[@]}" -v 2>&1 | tee -a "$LOG_FILE"
RETCODE=${PIPESTATUS[0]}
set -e

if [[ "$RETCODE" -eq 0 ]]; then
  write_status passed 0
  printf '=== %s PASSED at %s ===\n' "$PROFILE" "$(date -u +%FT%TZ)" | tee -a "$LOG_FILE"
else
  write_status failed "$RETCODE"
  printf '=== %s FAILED (rc=%s) at %s ===\n' "$PROFILE" "$RETCODE" "$(date -u +%FT%TZ)" | tee -a "$LOG_FILE"
fi
exit "$RETCODE"
