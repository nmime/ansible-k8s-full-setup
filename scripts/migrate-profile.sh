#!/usr/bin/env bash
# Resumable, backup-gated migration between every named platform profile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# Prefer the repository-managed Ansible runtime when the caller has not
# activated it explicitly (for example Codex desktop, CI subprocesses, or a
# direct ./scripts/migrate-profile.sh invocation).
if ! command -v ansible-playbook >/dev/null 2>&1 && [[ -x "${PROJECT_ROOT}/.venv/bin/ansible-playbook" ]]; then
  export PATH="${PROJECT_ROOT}/.venv/bin:${PATH}"
fi
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
TARGET_PROFILE=""
TARGET_EXPLICIT=false
COMMAND=""
DRY_RUN=false
FORCE=false
DR_ENDPOINT="${BACKUP_DR_ENDPOINT:-}"
DR_BUCKET="${BACKUP_DR_BUCKET:-}"
DR_REGION="${BACKUP_DR_REGION:-us-east-1}"
DR_PREFIX="${BACKUP_DR_PREFIX:-}"
BACKUP_RECIPIENT="${CLUSTER_BACKUP_AGE_RECIPIENT:-}"
OPERATOR_STATE_ROOT="${PROFILE_MIGRATION_OPERATOR_STATE_ROOT:-}"
SECRETS_FILE="${PROFILE_MIGRATION_SECRETS_FILE:-}"
VAULT_INIT_FILE="${PROFILE_MIGRATION_VAULT_INIT_FILE:-}"
SSH_KEY_PATH="${PROFILE_MIGRATION_SSH_KEY_PATH:-}"
SSH_KNOWN_HOSTS_FILE="${PROFILE_MIGRATION_SSH_KNOWN_HOSTS_FILE:-}"
K8S_API_LOCAL_PORT="${PROFILE_MIGRATION_K8S_API_LOCAL_PORT:-}"
VOLUME_QUOTA_GIB="${PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB:-}"
VOLUME_SAFETY_MARGIN_GIB="${PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB:-100}"
VOLUME_QUOTA_EXPLICIT=false
VOLUME_SAFETY_MARGIN_EXPLICIT=false
OPERATOR_STATE_ROOT_EXPLICIT=false
SECRETS_FILE_EXPLICIT=false
VAULT_INIT_FILE_EXPLICIT=false
SSH_KEY_PATH_EXPLICIT=false
SSH_KNOWN_HOSTS_FILE_EXPLICIT=false
K8S_API_LOCAL_PORT_EXPLICIT=false
[[ -z "$OPERATOR_STATE_ROOT" ]] || OPERATOR_STATE_ROOT_EXPLICIT=true
[[ -z "$SECRETS_FILE" ]] || SECRETS_FILE_EXPLICIT=true
[[ -z "$VAULT_INIT_FILE" ]] || VAULT_INIT_FILE_EXPLICIT=true
[[ -z "$SSH_KEY_PATH" ]] || SSH_KEY_PATH_EXPLICIT=true
[[ -z "$SSH_KNOWN_HOSTS_FILE" ]] || SSH_KNOWN_HOSTS_FILE_EXPLICIT=true
[[ -z "$K8S_API_LOCAL_PORT" ]] || K8S_API_LOCAL_PORT_EXPLICIT=true
[[ -z "$VOLUME_QUOTA_GIB" ]] || VOLUME_QUOTA_EXPLICIT=true
[[ -z "${PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB+x}" ]] || VOLUME_SAFETY_MARGIN_EXPLICIT=true
RESIZE_TIMEOUT="${PROFILE_MIGRATION_RESIZE_TIMEOUT:-900s}"
HCLOUD_CLIENT_TIMEOUT="${PROFILE_MIGRATION_HCLOUD_CLIENT_TIMEOUT_SECONDS:-900}"
HCLOUD_STATE_TIMEOUT="${PROFILE_MIGRATION_HCLOUD_STATE_TIMEOUT_SECONDS:-7200}"
HCLOUD_CAPACITY_RETRY_ATTEMPTS="${PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_ATTEMPTS:-12}"
HCLOUD_CAPACITY_RETRY_INTERVAL="${PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_INTERVAL_SECONDS:-15}"
HCLOUD_EQUIVALENT_FALLBACK_TYPES="${PROFILE_MIGRATION_HCLOUD_EQUIVALENT_FALLBACK_TYPES:-}"
ETCD_HEALTH_TIMEOUT="${PROFILE_MIGRATION_ETCD_HEALTH_TIMEOUT_SECONDS:-300}"
API_READY_TIMEOUT="${PROFILE_MIGRATION_API_READY_TIMEOUT_SECONDS:-300}"
CSI_DETACH_TIMEOUT="${PROFILE_MIGRATION_CSI_DETACH_TIMEOUT_SECONDS:-900}"
VAULT_MEMBER_TIMEOUT="${PROFILE_MIGRATION_VAULT_MEMBER_TIMEOUT_SECONDS:-900}"
PLATFORM_CONVERGENCE_TIMEOUT="${PROFILE_MIGRATION_PLATFORM_CONVERGENCE_TIMEOUT_SECONDS:-900}"
PLATFORM_CONVERGENCE_INTERVAL="${PROFILE_MIGRATION_PLATFORM_CONVERGENCE_INTERVAL_SECONDS:-15}"
ROOT_DISK_PRUNE_PERCENT="${PROFILE_MIGRATION_ROOT_DISK_PRUNE_PERCENT:-75}"
ROOT_DISK_MAX_PERCENT="${PROFILE_MIGRATION_ROOT_DISK_MAX_PERCENT:-85}"
VMCTL_IMAGE="docker.io/victoriametrics/vmctl:v1.147.0"
NAMED_PROFILES=(minimal small medium medium-optimized production)
STAGES=(preflight backup expand resize migrate-vault-storage apply-target migrate-data validate post-backup)
FINALIZE_STAGES=(retire-services retire-observability scale-in reconcile-target final-backup retire-backup cleanup-cloud validate-final)

log() { printf '[profile-migration] %s\n' "$*"; }
warn() { printf '[profile-migration] WARNING: %s\n' "$*" >&2; }
fail() { printf '[profile-migration] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[profile-migration] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: migrate-profile.sh [OPTIONS] <command>

Commands:
  plan       Generate and validate an all-to-all profile migration plan
  execute    Start a migration; refuses an existing incomplete migration
  resume     Continue from the first incomplete durable checkpoint
  status     Show migration and finalization checkpoint state
  rollback   Return to source capabilities before destructive finalization
  finalize   Retire disabled services, old data topology, and excess nodes

Options:
  --config FILE          Active platform YAML
  --target PROFILE       minimal|small|medium|medium-optimized|production
  --dr-endpoint URL      Independent S3-compatible Velero endpoint
  --dr-bucket NAME       Independent disaster-recovery bucket
  --dr-region NAME       S3 region (default: us-east-1)
  --dr-prefix PREFIX     Velero prefix (default: <project>/velero)
  --backup-recipient ID  age recipient for encrypted cluster bundles
  --operator-state-root DIR
                         Per-cluster generated-state directory; derives the
                         secrets and Vault init paths used by run_tier.sh
  --secrets-file FILE    Exact generated secrets file for this cluster
  --vault-init-file FILE Exact encrypted Vault init recovery file
  --ssh-key-path FILE    Exact private SSH identity used for cluster nodes
  --ssh-known-hosts FILE Project-isolated SSH host-key database
  --api-port PORT        Controller-local Kubernetes API tunnel port
  --volume-quota-gib N   Explicit Hetzner account volume quota in GiB; required
                         for live execute because the provider has no quota API
  --volume-safety-margin-gib N
                         Unallocated capacity retained after the estimated peak
                         (default: 100 GiB)
  --dry-run              Show pending stages without mutation
  --force                Skip interactive MIGRATE/FINALIZE confirmations
  -h, --help             Show this help

All 20 distinct transitions between the five named profiles are supported.
The workflow takes independent backups, expands before resizing, changes one
retained node at a time, verifies etcd around every control-plane change,
migrates VictoriaMetrics in either direction, and defers every destructive
scale-in or data retirement action to the separately confirmed finalize phase.
Existing PVC requests are never shrunk in place; safe larger requests are
recorded in the generated target config while obsolete PVCs are retired.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --target) TARGET_PROFILE="$2"; TARGET_EXPLICIT=true; shift 2 ;;
    --dr-endpoint) DR_ENDPOINT="$2"; shift 2 ;;
    --dr-bucket) DR_BUCKET="$2"; shift 2 ;;
    --dr-region) DR_REGION="$2"; shift 2 ;;
    --dr-prefix) DR_PREFIX="$2"; shift 2 ;;
    --backup-recipient) BACKUP_RECIPIENT="$2"; shift 2 ;;
    --operator-state-root) OPERATOR_STATE_ROOT="$2"; OPERATOR_STATE_ROOT_EXPLICIT=true; shift 2 ;;
    --secrets-file) SECRETS_FILE="$2"; SECRETS_FILE_EXPLICIT=true; shift 2 ;;
    --vault-init-file) VAULT_INIT_FILE="$2"; VAULT_INIT_FILE_EXPLICIT=true; shift 2 ;;
    --ssh-key-path) SSH_KEY_PATH="$2"; SSH_KEY_PATH_EXPLICIT=true; shift 2 ;;
    --ssh-known-hosts) SSH_KNOWN_HOSTS_FILE="$2"; SSH_KNOWN_HOSTS_FILE_EXPLICIT=true; shift 2 ;;
    --api-port) K8S_API_LOCAL_PORT="$2"; K8S_API_LOCAL_PORT_EXPLICIT=true; shift 2 ;;
    --volume-quota-gib) VOLUME_QUOTA_GIB="$2"; VOLUME_QUOTA_EXPLICIT=true; shift 2 ;;
    --volume-safety-margin-gib) VOLUME_SAFETY_MARGIN_GIB="$2"; VOLUME_SAFETY_MARGIN_EXPLICIT=true; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    plan|execute|resume|status|rollback|finalize)
      [[ -z "$COMMAND" ]] || fail "multiple commands were supplied"
      COMMAND="$1"; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done
[[ -n "$COMMAND" ]] || fail "a command is required"
[[ -f "$CONFIG_FILE" ]] || fail "platform config not found: $CONFIG_FILE"
CONFIG_FILE="$(cd "$(dirname "$CONFIG_FILE")" && pwd)/$(basename "$CONFIG_FILE")"
for tool in yq jq ansible-playbook; do command -v "$tool" >/dev/null || fail "required tool is missing: $tool"; done
[[ "$VOLUME_SAFETY_MARGIN_GIB" =~ ^[0-9]+$ ]] \
  || fail "--volume-safety-margin-gib must be a non-negative integer"
[[ -z "$VOLUME_QUOTA_GIB" || "$VOLUME_QUOTA_GIB" =~ ^[1-9][0-9]*$ ]] \
  || fail "--volume-quota-gib must be a positive integer"
[[ "$PLATFORM_CONVERGENCE_TIMEOUT" =~ ^[1-9][0-9]*$ ]] \
  || fail "PROFILE_MIGRATION_PLATFORM_CONVERGENCE_TIMEOUT_SECONDS must be a positive integer"
[[ "$PLATFORM_CONVERGENCE_INTERVAL" =~ ^[1-9][0-9]*$ ]] \
  || fail "PROFILE_MIGRATION_PLATFORM_CONVERGENCE_INTERVAL_SECONDS must be a positive integer"
[[ "$HCLOUD_CAPACITY_RETRY_ATTEMPTS" =~ ^[1-9][0-9]*$ ]] \
  || fail "PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_ATTEMPTS must be a positive integer"
[[ "$HCLOUD_CAPACITY_RETRY_INTERVAL" =~ ^[1-9][0-9]*$ ]] \
  || fail "PROFILE_MIGRATION_HCLOUD_CAPACITY_RETRY_INTERVAL_SECONDS must be a positive integer"
[[ "$CSI_DETACH_TIMEOUT" =~ ^[1-9][0-9]*$ ]] \
  || fail "PROFILE_MIGRATION_CSI_DETACH_TIMEOUT_SECONDS must be a positive integer"

is_named_profile() {
  local candidate="$1" profile
  for profile in "${NAMED_PROFILES[@]}"; do [[ "$candidate" != "$profile" ]] || return 0; done
  return 1
}

ACTIVE_PROFILE=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
PROJECT=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
DOMAIN=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
EMAIL=$(yq -r '.global.email // ""' "$CONFIG_FILE")
[[ -n "$DR_PREFIX" ]] || DR_PREFIX="${PROJECT}/velero"

absolute_existing_parent_path() {
  local path="$1" parent base
  parent=$(dirname "$path")
  base=$(basename "$path")
  [[ -d "$parent" ]] || fail "operator-state parent directory is missing: $parent"
  parent=$(cd "$parent" && pwd)
  printf '%s/%s\n' "$parent" "$base"
}

resolve_ssh_runtime_paths() {
  if [[ -z "$SSH_KEY_PATH" ]]; then
    SSH_KEY_PATH=$(yq -r '.infrastructure.ssh_key_path // ""' "$CONFIG_FILE")
  fi
  if [[ -z "$SSH_KEY_PATH" ]]; then
    SSH_KEY_PATH=$(yq -r '.ssh_key_path // "~/.ssh/id_ed25519"' "$PROJECT_ROOT/defaults/main.yml")
  fi
  [[ "${SSH_KEY_PATH#\~/}" == "$SSH_KEY_PATH" ]] \
    || SSH_KEY_PATH="${HOME:?HOME is required to resolve the SSH identity}/${SSH_KEY_PATH:2}"
  [[ "$SSH_KEY_PATH" == /* ]] || SSH_KEY_PATH="$(pwd)/${SSH_KEY_PATH}"

  if [[ -z "$SSH_KNOWN_HOSTS_FILE" ]]; then
    SSH_KNOWN_HOSTS_FILE="${HOME:?HOME is required for isolated SSH state}/.ssh/known_hosts-${PROJECT}"
  fi
  [[ "${SSH_KNOWN_HOSTS_FILE#\~/}" == "$SSH_KNOWN_HOSTS_FILE" ]] \
    || SSH_KNOWN_HOSTS_FILE="${HOME}/${SSH_KNOWN_HOSTS_FILE:2}"
  [[ "$SSH_KNOWN_HOSTS_FILE" == /* ]] || SSH_KNOWN_HOSTS_FILE="$(pwd)/${SSH_KNOWN_HOSTS_FILE}"

  if [[ -z "$K8S_API_LOCAL_PORT" ]]; then
    K8S_API_LOCAL_PORT=$(yq -r '.k8s_api_local_port // 16443' "$CONFIG_FILE")
  fi
  if [[ ! "$K8S_API_LOCAL_PORT" =~ ^[0-9]+$ ]] \
    || (( K8S_API_LOCAL_PORT < 1024 || K8S_API_LOCAL_PORT > 65535 )); then
    fail "--api-port must be an integer between 1024 and 65535"
  fi
}

if [[ -n "$OPERATOR_STATE_ROOT" ]]; then
  [[ -d "$OPERATOR_STATE_ROOT" ]] || fail "operator-state root is missing: $OPERATOR_STATE_ROOT"
  OPERATOR_STATE_ROOT=$(cd "$OPERATOR_STATE_ROOT" && pwd)
  if [[ -z "$SECRETS_FILE" ]]; then
    SECRETS_FILE="${OPERATOR_STATE_ROOT}/.platform-secrets.yml"
    SECRETS_FILE_EXPLICIT=true
  fi
  if [[ -z "$VAULT_INIT_FILE" ]]; then
    VAULT_INIT_FILE="${OPERATOR_STATE_ROOT}/.vault-init-${PROJECT}.json"
    VAULT_INIT_FILE_EXPLICIT=true
  fi
fi
[[ -n "$SECRETS_FILE" ]] || SECRETS_FILE="${PROJECT_ROOT}/playbooks/.platform-secrets.yml"
[[ -n "$VAULT_INIT_FILE" ]] || VAULT_INIT_FILE="${PROJECT_ROOT}/playbooks/.vault-init-${PROJECT}.json"
SECRETS_FILE=$(absolute_existing_parent_path "$SECRETS_FILE")
VAULT_INIT_FILE=$(absolute_existing_parent_path "$VAULT_INIT_FILE")
resolve_ssh_runtime_paths

STATE_BASE="${PROFILE_MIGRATION_STATE_DIR:-${PROJECT_ROOT}/.migration-state}"
POINTER_FILE="${STATE_BASE}/${PROJECT}-active-profile-migration"
SOURCE_PROFILE=""
STATE_DIR=""

discover_state() {
  local candidate state_file state_project newest=""
  if [[ -f "$POINTER_FILE" ]]; then
    candidate=$(<"$POINTER_FILE")
    if [[ -f "$candidate/state.json" ]]; then STATE_DIR="$candidate"; return 0; fi
  fi
  while IFS= read -r state_file; do
    candidate=$(dirname "$state_file")
    state_project=$(jq -r '.project // ""' "$state_file" 2>/dev/null || true)
    [[ "$state_project" == "$PROJECT" ]] || continue
    newest="$candidate"
  done < <(find "$STATE_BASE" -mindepth 1 -maxdepth 2 -type f -name state.json -print 2>/dev/null | sort)
  [[ -n "$newest" ]] || return 1
  STATE_DIR="$newest"
}

if [[ "$COMMAND" == plan || "$COMMAND" == execute ]]; then
  [[ "$TARGET_EXPLICIT" == true ]] || fail "--target is required for plan and execute"
  is_named_profile "$ACTIVE_PROFILE" || fail "source must be a named profile; current profile is $ACTIVE_PROFILE"
  is_named_profile "$TARGET_PROFILE" || fail "unknown target profile: $TARGET_PROFILE"
  [[ "$ACTIVE_PROFILE" != "$TARGET_PROFILE" ]] || fail "source and target profiles are already $TARGET_PROFILE"
  SOURCE_PROFILE="$ACTIVE_PROFILE"
  suffix="${PROJECT}-${SOURCE_PROFILE}-to-${TARGET_PROFILE}"
  if [[ "$COMMAND" == plan ]]; then suffix+="-plan"; elif [[ "$COMMAND" == execute && "$DRY_RUN" == true ]]; then suffix+="-dry-run"; fi
  STATE_DIR="${STATE_BASE}/${suffix}"
else
  if ! discover_state; then
    [[ "$COMMAND" == status ]] && { log "no migration state for $PROJECT"; exit 0; }
    fail "no migration state exists for $PROJECT"
  fi
  SOURCE_PROFILE=$(jq -r '.source_profile' "$STATE_DIR/state.json")
  state_target=$(jq -r '.target_profile' "$STATE_DIR/state.json")
  if [[ "$TARGET_EXPLICIT" == true && "$TARGET_PROFILE" != "$state_target" ]]; then
    fail "--target $TARGET_PROFILE does not match active migration target $state_target"
  fi
  TARGET_PROFILE="$state_target"
fi

STATE_FILE="${STATE_DIR}/state.json"
SOURCE_CONFIG="${STATE_DIR}/source-platform.yaml"
TARGET_CONFIG="${STATE_DIR}/target-platform.yaml"
STEADY_CONFIG="${STATE_DIR}/target-transition-platform.yaml"
EXPANSION_CONFIG="${STATE_DIR}/expansion-platform.yaml"
BACKUP_CONFIG="${STATE_DIR}/backup-platform.yaml"
POST_BACKUP_CONFIG="${STATE_DIR}/post-target-backup-platform.yaml"

MIGRATION_LOCK="${STATE_BASE}/.${PROJECT}-profile-migration.lock"
if [[ "$COMMAND" != plan && "$COMMAND" != status && "$DRY_RUN" != true ]]; then
  mkdir -p "$STATE_BASE"
  if ! mkdir "$MIGRATION_LOCK" 2>/dev/null; then
    if [[ -f "$MIGRATION_LOCK/pid" ]]; then
      lock_pid=$(<"$MIGRATION_LOCK/pid")
    else
      lock_pid=""
    fi
    if [[ "$lock_pid" =~ ^[0-9]+$ ]] && kill -0 "$lock_pid" 2>/dev/null; then
      fail "another migration process is active for $PROJECT (PID $lock_pid)"
    fi
    warn "removing stale migration lock for $PROJECT"
    rm -rf "$MIGRATION_LOCK"
    mkdir "$MIGRATION_LOCK" || fail "could not acquire migration lock for $PROJECT"
  fi
  printf '%s\n' "$$" > "$MIGRATION_LOCK/pid"
  trap 'rm -rf "$MIGRATION_LOCK"' EXIT INT TERM
fi
ROLLBACK_CONFIG="${STATE_DIR}/rollback-platform.yaml"
STORAGE_RETENTION_FILE="${STATE_DIR}/storage-retention.tsv"
STATEFUL_RETENTION_FILE="${STATE_DIR}/stateful-retention.tsv"
NODE_TYPE_RETENTION_FILE="${STATE_DIR}/node-type-retention.tsv"
BASTION_TYPE_RETENTION_FILE="${STATE_DIR}/bastion-type-retention.tsv"
NODE_TYPE_OVERRIDE_FILE="${STATE_DIR}/node-type-overrides.tsv"
SELECTION_RETENTION_FILE="${STATE_DIR}/selection-retention.tsv"
CAPACITY_PLAN_FILE="${STATE_DIR}/volume-capacity-plan.json"

restore_persisted_operator_state() {
  local recorded_root recorded_secrets recorded_vault recorded_ssh_key recorded_known_hosts recorded_api_port
  [[ -f "$STATE_FILE" ]] || return 0
  recorded_root=$(jq -r '.operator_state.root // ""' "$STATE_FILE")
  recorded_secrets=$(jq -r '.operator_state.secrets_file // ""' "$STATE_FILE")
  recorded_vault=$(jq -r '.operator_state.vault_init_file // ""' "$STATE_FILE")
  recorded_ssh_key=$(jq -r '.operator_state.ssh_key_path // ""' "$STATE_FILE")
  recorded_known_hosts=$(jq -r '.operator_state.ssh_known_hosts_file // ""' "$STATE_FILE")
  recorded_api_port=$(jq -r '.operator_state.k8s_api_local_port // ""' "$STATE_FILE")

  if [[ "$OPERATOR_STATE_ROOT_EXPLICIT" == true && -n "$recorded_root" && "$OPERATOR_STATE_ROOT" != "$recorded_root" ]]; then
    fail "--operator-state-root does not match the active migration state: $recorded_root"
  fi
  if [[ "$SECRETS_FILE_EXPLICIT" == true && -n "$recorded_secrets" && "$SECRETS_FILE" != "$recorded_secrets" ]]; then
    fail "--secrets-file does not match the active migration state: $recorded_secrets"
  fi
  if [[ "$VAULT_INIT_FILE_EXPLICIT" == true && -n "$recorded_vault" && "$VAULT_INIT_FILE" != "$recorded_vault" ]]; then
    fail "--vault-init-file does not match the active migration state: $recorded_vault"
  fi
  if [[ "$SSH_KEY_PATH_EXPLICIT" == true && -n "$recorded_ssh_key" && "$SSH_KEY_PATH" != "$recorded_ssh_key" ]]; then
    fail "--ssh-key-path does not match the active migration state: $recorded_ssh_key"
  fi
  if [[ "$SSH_KNOWN_HOSTS_FILE_EXPLICIT" == true && -n "$recorded_known_hosts" && "$SSH_KNOWN_HOSTS_FILE" != "$recorded_known_hosts" ]]; then
    fail "--ssh-known-hosts does not match the active migration state: $recorded_known_hosts"
  fi
  if [[ "$K8S_API_LOCAL_PORT_EXPLICIT" == true && -n "$recorded_api_port" && "$K8S_API_LOCAL_PORT" != "$recorded_api_port" ]]; then
    fail "--api-port does not match the active migration state: $recorded_api_port"
  fi

  [[ -z "$recorded_root" || "$OPERATOR_STATE_ROOT_EXPLICIT" == true ]] || OPERATOR_STATE_ROOT="$recorded_root"
  [[ -z "$recorded_secrets" || "$SECRETS_FILE_EXPLICIT" == true ]] || SECRETS_FILE="$recorded_secrets"
  [[ -z "$recorded_vault" || "$VAULT_INIT_FILE_EXPLICIT" == true ]] || VAULT_INIT_FILE="$recorded_vault"
  [[ -z "$recorded_ssh_key" || "$SSH_KEY_PATH_EXPLICIT" == true ]] || SSH_KEY_PATH="$recorded_ssh_key"
  [[ -z "$recorded_known_hosts" || "$SSH_KNOWN_HOSTS_FILE_EXPLICIT" == true ]] || SSH_KNOWN_HOSTS_FILE="$recorded_known_hosts"
  [[ -z "$recorded_api_port" || "$K8S_API_LOCAL_PORT_EXPLICIT" == true ]] || K8S_API_LOCAL_PORT="$recorded_api_port"
  if [[ ! "$K8S_API_LOCAL_PORT" =~ ^[0-9]+$ ]] \
    || (( K8S_API_LOCAL_PORT < 1024 || K8S_API_LOCAL_PORT > 65535 )); then
    fail "recorded Kubernetes API port is not between 1024 and 65535: $K8S_API_LOCAL_PORT"
  fi
}

restore_persisted_volume_settings() {
  local recorded_quota recorded_margin
  [[ -f "$STATE_FILE" ]] || return 0
  recorded_quota=$(jq -r '.volume_capacity.quota_gib // ""' "$STATE_FILE")
  recorded_margin=$(jq -r '.volume_capacity.safety_margin_gib // ""' "$STATE_FILE")
  if [[ "$VOLUME_QUOTA_EXPLICIT" == true && -n "$recorded_quota" && "$VOLUME_QUOTA_GIB" != "$recorded_quota" ]]; then
    fail "--volume-quota-gib does not match the active migration state: $recorded_quota"
  fi
  if [[ "$VOLUME_SAFETY_MARGIN_EXPLICIT" == true && -n "$recorded_margin" && "$VOLUME_SAFETY_MARGIN_GIB" != "$recorded_margin" ]]; then
    fail "--volume-safety-margin-gib does not match the active migration state: $recorded_margin"
  fi
  [[ -n "$VOLUME_QUOTA_GIB" || -z "$recorded_quota" ]] || VOLUME_QUOTA_GIB="$recorded_quota"
  [[ "$VOLUME_SAFETY_MARGIN_EXPLICIT" == true || -z "$recorded_margin" ]] || VOLUME_SAFETY_MARGIN_GIB="$recorded_margin"
}

validate_operator_state_inputs() {
  [[ -f "$SECRETS_FILE" && -r "$SECRETS_FILE" && -s "$SECRETS_FILE" ]] \
    || fail "exact generated secrets file is missing, unreadable, or empty: $SECRETS_FILE"
  if [[ $(yq -r '.secrets.enabled // false' "$SOURCE_CONFIG") == true ]]; then
    [[ -f "$VAULT_INIT_FILE" && -r "$VAULT_INIT_FILE" && -s "$VAULT_INIT_FILE" ]] \
      || fail "exact encrypted Vault init file is missing, unreadable, or empty: $VAULT_INIT_FILE"
  fi
  [[ -f "$SSH_KEY_PATH" && ! -L "$SSH_KEY_PATH" && -r "$SSH_KEY_PATH" ]] \
    || fail "exact private SSH identity is missing, unreadable, or a symlink: $SSH_KEY_PATH"
  [[ -f "${SSH_KEY_PATH}.pub" && ! -L "${SSH_KEY_PATH}.pub" && -r "${SSH_KEY_PATH}.pub" ]] \
    || fail "public SSH identity is missing, unreadable, or a symlink: ${SSH_KEY_PATH}.pub"
  [[ -f "$SSH_KNOWN_HOSTS_FILE" && ! -L "$SSH_KNOWN_HOSTS_FILE" && -r "$SSH_KNOWN_HOSTS_FILE" ]] \
    || fail "project SSH known-hosts file is missing, unreadable, or a symlink: $SSH_KNOWN_HOSTS_FILE"
}

persist_controller_runtime_state() {
  local tmp="${STATE_FILE}.tmp.$$"
  jq --arg sshKey "$SSH_KEY_PATH" --arg knownHosts "$SSH_KNOWN_HOSTS_FILE" \
    --argjson apiPort "$K8S_API_LOCAL_PORT" \
    '.operator_state.ssh_key_path=$sshKey |
      .operator_state.ssh_known_hosts_file=$knownHosts |
      .operator_state.k8s_api_local_port=$apiPort |
      .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

validate_volume_capacity_settings() {
  [[ "$VOLUME_QUOTA_GIB" =~ ^[1-9][0-9]*$ ]] \
    || fail "set an explicit positive Hetzner account volume quota with --volume-quota-gib or PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB"
  [[ "$VOLUME_SAFETY_MARGIN_GIB" =~ ^[0-9]+$ ]] \
    || fail "--volume-safety-margin-gib must be a non-negative integer"
  (( VOLUME_SAFETY_MARGIN_GIB < VOLUME_QUOTA_GIB )) \
    || fail "volume safety margin must be smaller than the account volume quota"
}

if [[ "$COMMAND" != plan && "$COMMAND" != execute ]]; then
  restore_persisted_operator_state
  restore_persisted_volume_settings
fi

state_status() {
  jq . "$STATE_FILE"
  local stage
  for stage in "${STAGES[@]}"; do
    if [[ -f "$STATE_DIR/stage-${stage}.done" ]]; then printf '  %-24s complete\n' "$stage"; else printf '  %-24s pending\n' "$stage"; fi
  done
  for stage in "${FINALIZE_STAGES[@]}"; do
    if [[ -f "$STATE_DIR/finalize-${stage}.done" ]]; then printf '  finalize/%-15s complete\n' "$stage"; else printf '  finalize/%-15s pending\n' "$stage"; fi
  done
}
[[ "$COMMAND" != status ]] || { state_status; exit 0; }

mkdir -p "$STATE_BASE" "$STATE_DIR"
chmod 700 "$STATE_BASE" "$STATE_DIR"

set_yaml_string() {
  local file="$1" path="$2" value="$3"
  VALUE="$value" yq -i "${path} = strenv(VALUE)" "$file"
}

profile_tier() { yq -r '.tier' "$1"; }
profile_mode() {
  case $(profile_tier "$1") in minimal|small) printf single ;; *) printf cluster ;; esac
}

quantity_mib() {
  local value="$1"
  awk -v q="$value" 'BEGIN {
    if (q ~ /^[0-9]+([.][0-9]+)?Ti$/) {sub(/Ti$/, "", q); print int(q*1048576); exit}
    if (q ~ /^[0-9]+([.][0-9]+)?Gi$/) {sub(/Gi$/, "", q); print int(q*1024); exit}
    if (q ~ /^[0-9]+([.][0-9]+)?Mi$/) {sub(/Mi$/, "", q); print int(q); exit}
    if (q ~ /^[0-9]+([.][0-9]+)?Ki$/) {sub(/Ki$/, "", q); print int(q/1024); exit}
    exit 2
  }'
}

resource_default() {
  local file="$1" kind="$2" tier resource
  tier=$(yq -r '.tier' "$file")
  resource=$(yq -r '.resource_tier' "$file")
  case "$kind:$resource:$tier" in
    metrics:minimal:*|metrics:small:*) printf 20Gi ;;
    metrics:medium:*) printf 100Gi ;;
    metrics:production:*) printf 150Gi ;;
    pmm:minimal:*|pmm:small:*) printf 20Gi ;;
    pmm:*) printf 50Gi ;;
    gitaly:minimal:*) printf 10Gi ;;
    gitaly:small:*) printf 20Gi ;;
    gitaly:*) printf 50Gi ;;
    es-master:production:*) printf 30Gi ;;
    es-master:*) printf 20Gi ;;
    es-data:production:*) printf 150Gi ;;
    es-data:medium:*) printf 100Gi ;;
    es-data:*) printf 20Gi ;;
    dragonfly:minimal:*|dragonfly:small:*) printf 10Gi ;;
    dragonfly:*) printf 20Gi ;;
    postal:minimal:*|postal:small:*) printf 20Gi ;;
    postal:*) printf 50Gi ;;
    coroot:minimal:*|coroot:small:*) printf 10Gi ;;
    coroot:medium:*) printf 20Gi ;;
    coroot:production:*) printf 40Gi ;;
    tempo:minimal:*|tempo:small:*) printf 10Gi ;;
    tempo:medium:*) printf 20Gi ;;
    tempo:production:*) printf 40Gi ;;
    *) printf 20Gi ;;
  esac
}

retain_larger_quantity() {
  local label="$1" source_expr="$2" target_expr="$3" target_path="$4" source_default="$5" target_default="$6"
  local source_value target_value source_mib target_mib
  source_value=$(yq -r "${source_expr} // \"${source_default}\"" "$SOURCE_CONFIG")
  target_value=$(yq -r "${target_expr} // \"${target_default}\"" "$TARGET_CONFIG")
  source_mib=$(quantity_mib "$source_value") || fail "unsupported source storage quantity for $label: $source_value"
  target_mib=$(quantity_mib "$target_value") || fail "unsupported target storage quantity for $label: $target_value"
  if (( source_mib > target_mib )); then
    set_yaml_string "$TARGET_CONFIG" "$target_path" "$source_value"
    printf '%s\t%s\t%s\t%s\n' "$label" "$source_value" "$target_value" "$target_path" >> "$STORAGE_RETENTION_FILE"
  fi
}

retain_larger_replica_count() {
  local label="$1" source_expr="$2" target_expr="$3" target_path="$4" source_default="$5" target_default="$6"
  local source_value target_value
  source_value=$(yq -r "${source_expr} // ${source_default}" "$SOURCE_CONFIG")
  target_value=$(yq -r "${target_expr} // ${target_default}" "$TARGET_CONFIG")
  [[ "$source_value" =~ ^[0-9]+$ && "$target_value" =~ ^[0-9]+$ ]] || fail "invalid replica count for $label: $source_value -> $target_value"
  if (( source_value > target_value )); then
    VALUE="$source_value" yq -i "${target_path} = (strenv(VALUE) | tonumber)" "$TARGET_CONFIG"
    printf '%s\t%s\t%s\t%s\n' "$label" "$source_value" "$target_value" "$target_path" >> "$STATEFUL_RETENTION_FILE"
  fi
}

component_path() {
  case "$1" in
    object-storage) printf '.storage.enabled' ;; secrets) printf '.secrets.enabled' ;; eso) printf '.secrets.eso.enabled' ;;
    databases) printf '.databases.enabled' ;; postgresql) printf '.databases.postgresql.enabled' ;; mongodb) printf '.databases.mongodb.enabled' ;;
    elasticsearch) printf '.elasticsearch.enabled' ;; dragonfly) printf '.dragonfly.enabled' ;; gitlab) printf '.gitlab.enabled' ;;
    gitlab-runner) printf '.gitlab.runner.enabled' ;; gitops) printf '.gitops.enabled' ;; observability) printf '.observability.enabled' ;;
    coroot) printf '.coroot.enabled' ;; tracing) printf '.tracing.enabled' ;; autoscaling) printf '.autoscaling.enabled' ;;
    temporal) printf '.temporal.enabled' ;; postal) printf '.postal.enabled' ;; backup) printf '.backup.enabled' ;;
    disaster-recovery) printf '.backup.disaster_recovery.enabled' ;;
    glitchtip) printf '.glitchtip.enabled' ;; apm) printf '.apm.enabled' ;; blackbox) printf '.blackbox.enabled' ;;
    daytona) printf '.applications.daytona.enabled' ;; hipaa) printf '.compliance.hipaa.enabled' ;; *) return 1 ;;
  esac
}

component_enabled() {
  local file="$1" component="$2" path
  path=$(component_path "$component")
  [[ $(yq -r "${path} // false" "$file") == true ]]
}

component_selection_paths() {
  cat <<'EOF'
.storage.enabled
.secrets.enabled
.secrets.eso.enabled
.databases.enabled
.databases.postgresql.enabled
.databases.mongodb.enabled
.elasticsearch.enabled
.dragonfly.enabled
.gitlab.enabled
.gitlab.runner.enabled
.gitops.enabled
.observability.enabled
.observability.pmm.enabled
.coroot.enabled
.tracing.enabled
.autoscaling.enabled
.temporal.enabled
.postal.enabled
.backup.enabled
.backup.disaster_recovery.enabled
.glitchtip.enabled
.apm.enabled
.blackbox.enabled
.applications.daytona.enabled
.compliance.hipaa.enabled
.alerting.telegram.enabled
.alerting.email.enabled
EOF
}

preserve_optional_selection_overrides() {
  local baseline="$PROJECT_ROOT/platform-orchestrator/profiles/${SOURCE_PROFILE}.yaml"
  local path source_value baseline_value
  [[ -f "$baseline" ]] || fail "source profile baseline is missing: $baseline"
  : > "$SELECTION_RETENTION_FILE"
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    source_value=$(yq -r "${path} // false" "$SOURCE_CONFIG")
    baseline_value=$(yq -r "${path} // false" "$baseline")
    if [[ "$source_value" != "$baseline_value" ]]; then
      SELECTION_VALUE="$source_value" yq -i \
        "${path} = (strenv(SELECTION_VALUE) == \"true\")" "$TARGET_CONFIG"
      printf '%s\t%s\t%s\n' "$path" "$baseline_value" "$source_value" >> "$SELECTION_RETENTION_FILE"
    fi
  done < <(component_selection_paths)
}

enforce_target_dependency_closure() {
  # A target profile can introduce dependants that did not exist in the source
  # profile. When the operator explicitly disabled a dependency in the source,
  # keep that choice authoritative by disabling the newly introduced dependants
  # instead of generating an invalid target selection.
  if ! component_enabled "$TARGET_CONFIG" object-storage; then
    yq -i '.gitlab.enabled = false | .gitlab.runner.enabled = false |
      .tracing.enabled = false | .backup.enabled = false |
      .backup.disaster_recovery.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" secrets; then
    yq -i '.secrets.eso.enabled = false | .compliance.hipaa.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" databases; then
    yq -i '.databases.postgresql.enabled = false | .databases.mongodb.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" postgresql; then
    yq -i '.gitlab.enabled = false | .gitlab.runner.enabled = false |
      .temporal.enabled = false | .glitchtip.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" elasticsearch; then
    yq -i '.apm.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" dragonfly; then
    yq -i '.gitlab.enabled = false | .gitlab.runner.enabled = false |
      .postal.enabled = false | .glitchtip.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" gitlab; then
    yq -i '.gitlab.runner.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" observability; then
    yq -i '.observability.pmm.enabled = false | .coroot.enabled = false |
      .tracing.enabled = false | .blackbox.enabled = false |
      .compliance.hipaa.enabled = false | .alerting.telegram.enabled = false |
      .alerting.email.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" postal; then
    yq -i '.alerting.email.enabled = false' "$TARGET_CONFIG"
  fi
  if ! component_enabled "$TARGET_CONFIG" backup; then
    yq -i '.backup.disaster_recovery.enabled = false' "$TARGET_CONFIG"
  fi
}

components_to_remove() {
  local component
  for component in daytona blackbox apm glitchtip temporal postal tracing coroot gitlab-runner gitlab mongodb eso elasticsearch dragonfly disaster-recovery backup autoscaling gitops observability postgresql databases secrets object-storage; do
    if component_enabled "$SOURCE_CONFIG" "$component" && ! component_enabled "$TARGET_CONFIG" "$component"; then printf '%s\n' "$component"; fi
  done
}

refuse_automatic_hipaa_retirement() {
  if component_enabled "$SOURCE_CONFIG" hipaa && ! component_enabled "$TARGET_CONFIG" hipaa; then
    fail "HIPAA-oriented hardening cannot be retired by profile migration; keep compliance.hipaa.enabled=true or complete a separately reviewed control-by-control reversal before migrating"
  fi
}

preserve_non_shrinking_storage() {
  : > "$STORAGE_RETENTION_FILE"
  : > "$STATEFUL_RETENTION_FILE"
  retain_larger_quantity seaweedfs-volume '.storage.size_per_replica // .storage.size' '.storage.size_per_replica // .storage.size' '.storage.size_per_replica' 50Gi 50Gi
  retain_larger_quantity seaweedfs-master '.storage.master_size' '.storage.master_size' '.storage.master_size' 4Gi 4Gi
  # The live role default is 4Gi. Using the profile's requested 2Gi as the
  # source fallback hides an immutable PVC shrink during compact -> HA moves.
  retain_larger_quantity seaweedfs-index '.storage.index_size' '.storage.index_size' '.storage.index_size' 4Gi 4Gi
  retain_larger_quantity seaweedfs-filer '.storage.filer_size' '.storage.filer_size' '.storage.filer_size' 10Gi 10Gi
  retain_larger_quantity vault '.secrets.vault.storage_size' '.secrets.vault.storage_size' '.secrets.vault.storage_size' 20Gi 20Gi
  retain_larger_quantity postgresql '.databases.postgresql.storage_size' '.databases.postgresql.storage_size' '.databases.postgresql.storage_size' 20Gi 20Gi
  if component_enabled "$SOURCE_CONFIG" mongodb && component_enabled "$TARGET_CONFIG" mongodb; then
    retain_larger_quantity mongodb '.databases.mongodb.storage_size' '.databases.mongodb.storage_size' '.databases.mongodb.storage_size' 20Gi 20Gi
  fi
  if component_enabled "$SOURCE_CONFIG" gitlab && component_enabled "$TARGET_CONFIG" gitlab; then
    retain_larger_quantity gitlab-gitaly '.gitlab.gitaly_storage_size' '.gitlab.gitaly_storage_size' '.gitlab.gitaly_storage_size' "$(resource_default "$SOURCE_CONFIG" gitaly)" "$(resource_default "$TARGET_CONFIG" gitaly)"
    retain_larger_quantity gitlab-backup '.gitlab.backup_persistence_size' '.gitlab.backup_persistence_size' '.gitlab.backup_persistence_size' 50Gi 50Gi
  fi
  if [[ $(profile_mode "$SOURCE_CONFIG") == $(profile_mode "$TARGET_CONFIG") ]]; then
    retain_larger_quantity victoriametrics '.observability.metrics.storage_size' '.observability.metrics.storage_size' '.observability.metrics.storage_size' "$(resource_default "$SOURCE_CONFIG" metrics)" "$(resource_default "$TARGET_CONFIG" metrics)"
  fi
  retain_larger_quantity pmm '.observability.pmm.storage_size' '.observability.pmm.storage_size' '.observability.pmm.storage_size' "$(resource_default "$SOURCE_CONFIG" pmm)" "$(resource_default "$TARGET_CONFIG" pmm)"
  if component_enabled "$SOURCE_CONFIG" observability && component_enabled "$TARGET_CONFIG" observability; then
    retain_larger_quantity alertmanager '.alerting.storage_size' '.alerting.storage_size' '.alerting.storage_size' 5Gi 5Gi
  fi
  if component_enabled "$SOURCE_CONFIG" elasticsearch && component_enabled "$TARGET_CONFIG" elasticsearch; then
    retain_larger_quantity elasticsearch-master '.elasticsearch.master.storage_size' '.elasticsearch.master.storage_size' '.elasticsearch.master.storage_size' "$(resource_default "$SOURCE_CONFIG" es-master)" "$(resource_default "$TARGET_CONFIG" es-master)"
    retain_larger_quantity elasticsearch-data '.elasticsearch.data.storage_size' '.elasticsearch.data.storage_size' '.elasticsearch.data.storage_size' "$(resource_default "$SOURCE_CONFIG" es-data)" "$(resource_default "$TARGET_CONFIG" es-data)"
  fi
  if component_enabled "$SOURCE_CONFIG" dragonfly && component_enabled "$TARGET_CONFIG" dragonfly; then
    retain_larger_quantity dragonfly '.dragonfly.snapshot_storage' '.dragonfly.snapshot_storage' '.dragonfly.snapshot_storage' "$(resource_default "$SOURCE_CONFIG" dragonfly)" "$(resource_default "$TARGET_CONFIG" dragonfly)"
  fi
  if component_enabled "$SOURCE_CONFIG" postal && component_enabled "$TARGET_CONFIG" postal; then
    retain_larger_quantity postal '.postal.mariadb_storage' '.postal.mariadb_storage' '.postal.mariadb_storage' "$(resource_default "$SOURCE_CONFIG" postal)" "$(resource_default "$TARGET_CONFIG" postal)"
  fi
  if component_enabled "$SOURCE_CONFIG" coroot && component_enabled "$TARGET_CONFIG" coroot; then
    retain_larger_quantity coroot '.coroot.storage_size' '.coroot.storage_size' '.coroot.storage_size' "$(resource_default "$SOURCE_CONFIG" coroot)" "$(resource_default "$TARGET_CONFIG" coroot)"
    retain_larger_quantity coroot-clickhouse '.coroot.clickhouse.storage_size' '.coroot.clickhouse.storage_size' '.coroot.clickhouse.storage_size' 50Gi 50Gi
  fi
  if component_enabled "$SOURCE_CONFIG" tracing && component_enabled "$TARGET_CONFIG" tracing; then
    retain_larger_quantity tempo '.tracing.storage_size' '.tracing.storage_size' '.tracing.storage_size' "$(resource_default "$SOURCE_CONFIG" tempo)" "$(resource_default "$TARGET_CONFIG" tempo)"
  fi
  retain_larger_replica_count seaweedfs-master '.storage.master_replicas // .storage.replicas' '.storage.master_replicas // .storage.replicas' '.storage.master_replicas' 1 1
  retain_larger_replica_count seaweedfs-volume '.storage.volume_replicas // .storage.replicas' '.storage.volume_replicas // .storage.replicas' '.storage.volume_replicas' 1 1
  retain_larger_replica_count seaweedfs-filer '.storage.filer_replicas' '.storage.filer_replicas' '.storage.filer_replicas' 1 1
  retain_larger_replica_count vault-raft '.secrets.vault.replicas' '.secrets.vault.replicas' '.secrets.vault.replicas' 1 1
  if [[ $(profile_mode "$SOURCE_CONFIG") == cluster && $(profile_mode "$TARGET_CONFIG") == cluster ]]; then
    retain_larger_replica_count victoriametrics-cluster '.observability.metrics.replicas' '.observability.metrics.replicas' '.observability.metrics.replicas' 2 2
    # Retaining the larger VMStorage replica count without its matching
    # replication factor creates a hybrid topology that stores new samples at
    # the target's lower durability. Preserve both halves of the HA contract.
    retain_larger_replica_count victoriametrics-replication-factor '.observability.metrics.replication_factor' '.observability.metrics.replication_factor' '.observability.metrics.replication_factor' 1 1
  fi
}

preserve_declared_bastion_type() {
  local source_enabled target_enabled source_type target_type
  source_enabled=$(yq -r '.network.bastion.enabled // false' "$SOURCE_CONFIG")
  target_enabled=$(yq -r '.network.bastion.enabled // false' "$TARGET_CONFIG")
  source_type=$(yq -r '.network.bastion.server_type // ""' "$SOURCE_CONFIG")
  target_type=$(yq -r '.network.bastion.server_type // ""' "$TARGET_CONFIG")
  [[ "$source_enabled" == "$target_enabled" ]] \
    || fail "profile migration has no safe bastion create/delete path; source enabled=${source_enabled}, target enabled=${target_enabled}"
  if [[ "$source_enabled" == true ]]; then
    [[ -n "$source_type" ]] \
      || fail "source bastion type is not declared; set network.bastion.server_type before migration"
    [[ -n "$target_type" ]] \
      || fail "target bastion type is not declared"
    set_yaml_string "$TARGET_CONFIG" '.network.bastion.server_type' "$source_type"
  fi
  printf 'source-declared\t%s\ttarget-requested\t%s\tretained\t%s\n' \
    "${source_type:-disabled}" "${target_type:-disabled}" "${source_type:-disabled}" \
    > "$BASTION_TYPE_RETENTION_FILE"
}

generate_configs() {
  cp "$CONFIG_FILE" "$SOURCE_CONFIG"
  set_yaml_string "$SOURCE_CONFIG" '.infrastructure.ssh_key_path' "$SSH_KEY_PATH"
  K8S_API_LOCAL_PORT_VALUE="$K8S_API_LOCAL_PORT" yq -i \
    '.k8s_api_local_port = (strenv(K8S_API_LOCAL_PORT_VALUE) | tonumber)' "$SOURCE_CONFIG"
  cp "$PROJECT_ROOT/platform-orchestrator/profiles/${TARGET_PROFILE}.yaml" "$TARGET_CONFIG"
  set_yaml_string "$TARGET_CONFIG" '.global.project' "$PROJECT"
  set_yaml_string "$TARGET_CONFIG" '.global.domain' "$DOMAIN"
  set_yaml_string "$TARGET_CONFIG" '.global.email' "$EMAIL"
  set_yaml_string "$TARGET_CONFIG" '.global.timezone' "$(yq -r '.global.timezone // "UTC"' "$SOURCE_CONFIG")"
  set_yaml_string "$TARGET_CONFIG" '.infrastructure.region' "$(yq -r '.infrastructure.region // "hel1"' "$SOURCE_CONFIG")"
  set_yaml_string "$TARGET_CONFIG" '.infrastructure.ssh_key_path' "$SSH_KEY_PATH"
  K8S_API_LOCAL_PORT_VALUE="$K8S_API_LOCAL_PORT" yq -i \
    '.k8s_api_local_port = (strenv(K8S_API_LOCAL_PORT_VALUE) | tonumber)' "$TARGET_CONFIG"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"
  preserve_declared_bastion_type
  preserve_optional_selection_overrides
  enforce_target_dependency_closure
  refuse_automatic_hipaa_retirement
  preserve_non_shrinking_storage

  source_cp=$(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG")
  source_workers=$(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG")
  target_cp=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  target_workers=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  (( source_cp > target_cp )) && transition_cp=$source_cp || transition_cp=$target_cp
  (( source_workers > target_workers )) && transition_workers=$source_workers || transition_workers=$target_workers

  cp "$SOURCE_CONFIG" "$BACKUP_CONFIG"
  yq -i '.platform_profile = "custom" | .backup.enabled = true | .backup.disaster_recovery.enabled = true' "$BACKUP_CONFIG"
  if [[ $(yq -r '.resource_tier // .tier // "custom"' "$SOURCE_CONFIG") =~ ^(minimal|small)$ ]]; then
    yq -i '
      .backup.job_resources.cpu_request = (.backup.job_resources.cpu_request // "10m") |
      .backup.job_resources.cpu_limit = (.backup.job_resources.cpu_limit // "250m") |
      .backup.job_resources.memory_request = (.backup.job_resources.memory_request // "64Mi") |
      .backup.job_resources.memory_limit = (.backup.job_resources.memory_limit // "256Mi")
    ' "$BACKUP_CONFIG"
  fi
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"
  yq -i '.backup.disaster_recovery.schedule = "30 2 * * *" | .backup.disaster_recovery.retention_hours = 720' "$BACKUP_CONFIG"

  cp "$SOURCE_CONFIG" "$EXPANSION_CONFIG"
  TRANSITION_CP="$transition_cp" TRANSITION_WORKERS="$transition_workers" yq -i '
    .platform_profile = "custom" |
    .kubernetes.ha_control_plane = ((strenv(TRANSITION_CP) | tonumber) > 1) |
    .infrastructure.control_plane.count = (strenv(TRANSITION_CP) | tonumber) |
    .infrastructure.workers.count = (strenv(TRANSITION_WORKERS) | tonumber)' "$EXPANSION_CONFIG"

  cp "$TARGET_CONFIG" "$STEADY_CONFIG"
  TRANSITION_CP="$transition_cp" TRANSITION_WORKERS="$transition_workers" yq -i '
    .platform_profile = "custom" |
    .infrastructure.control_plane.count = (strenv(TRANSITION_CP) | tonumber) |
    .infrastructure.workers.count = (strenv(TRANSITION_WORKERS) | tonumber)' "$STEADY_CONFIG"

  cp "$SOURCE_CONFIG" "$ROLLBACK_CONFIG"
  target_cp_type=$(yq -r '.infrastructure.control_plane.type' "$TARGET_CONFIG")
  target_worker_type=$(yq -r '.infrastructure.workers.type' "$TARGET_CONFIG")
  SOURCE_PROFILE_VALUE="$SOURCE_PROFILE" TRANSITION_CP="$transition_cp" TRANSITION_WORKERS="$transition_workers" yq -i '
    .platform_profile = strenv(SOURCE_PROFILE_VALUE) |
    .infrastructure.control_plane.count = (strenv(TRANSITION_CP) | tonumber) |
    .infrastructure.workers.count = (strenv(TRANSITION_WORKERS) | tonumber)' "$ROLLBACK_CONFIG"
  set_yaml_string "$ROLLBACK_CONFIG" '.infrastructure.control_plane.type' "$target_cp_type"
  set_yaml_string "$ROLLBACK_CONFIG" '.infrastructure.workers.type' "$target_worker_type"
  "$SCRIPT_DIR/profile-storage-capacity.py" \
    --source "$SOURCE_CONFIG" --target "$TARGET_CONFIG" > "$CAPACITY_PLAN_FILE"
  capacity_quota_json=null
  [[ "$VOLUME_QUOTA_GIB" =~ ^[1-9][0-9]*$ ]] && capacity_quota_json="$VOLUME_QUOTA_GIB"
  jq --argjson quota "$capacity_quota_json" \
    --argjson margin "$VOLUME_SAFETY_MARGIN_GIB" '
      .planning_inputs={configured_account_quota_gib:$quota,
        safety_margin_gib:$margin,live_account_usage_required:true} |
      .minimum_required_headroom_gib=(.required_additional_gib + $margin) |
      .offline_result=(
        if $quota == null then "quota-required-before-execute"
        elif .minimum_required_headroom_gib > $quota then "impossible-even-empty-account"
        else "requires-live-provider-state" end)
    ' "$CAPACITY_PLAN_FILE" > "${CAPACITY_PLAN_FILE}.tmp.$$"
  mv "${CAPACITY_PLAN_FILE}.tmp.$$" "$CAPACITY_PLAN_FILE"
  jq -e '
    .schema_version == 1 and
    (.source.persistent_total_gib | numbers) and
    (.target.persistent_total_gib | numbers) and
    (.target_delta_gib | numbers) and
    (.migration_scratch_gib | numbers) and
    (.required_additional_gib == (.target_delta_gib + .migration_scratch_gib)) and
    (.minimum_required_headroom_gib ==
      (.required_additional_gib + .planning_inputs.safety_margin_gib))
  ' "$CAPACITY_PLAN_FILE" >/dev/null \
    || fail "generated volume-capacity plan is invalid"
}

validate_generated_configs() {
  local generated
  if [[ ${PROFILE_MIGRATION_SKIP_ANSIBLE_VALIDATION:-false} == true ]]; then
    [[ "$COMMAND" == plan ]] || fail "PROFILE_MIGRATION_SKIP_ANSIBLE_VALIDATION is permitted only for non-mutating plan tests"
    warn "skipping generated-config Ansible validation by explicit environment override"
    return 0
  fi
  for generated in "$TARGET_CONFIG" "$STEADY_CONFIG" "$EXPANSION_CONFIG" "$BACKUP_CONFIG" "$ROLLBACK_CONFIG"; do
    ansible-playbook "$PROJECT_ROOT/playbooks/validate_profile.yml" -e "@$generated" >/dev/null
  done
}

archive_reusable_state() {
  local status archived
  [[ -f "$STATE_FILE" ]] || return 0
  status=$(jq -r '.status' "$STATE_FILE")
  case "$status" in
    finalized|rolled_back)
      archived="${STATE_DIR}-$(date -u +%Y%m%dT%H%M%SZ)"
      mv "$STATE_DIR" "$archived"
      mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
      ;;
    *) fail "migration state already exists with status=$status; use resume, status, rollback, or finalize" ;;
  esac
}

if [[ "$COMMAND" == plan ]]; then
  [[ ! -f "$STATE_FILE" ]] || rm -rf "$STATE_DIR"
  mkdir -p "$STATE_DIR"; chmod 700 "$STATE_DIR"
  generate_configs
  validate_generated_configs
  log "validated ${SOURCE_PROFILE} -> ${TARGET_PROFILE} migration plan"
  cat <<EOF

Stages: ${STAGES[*]}
Finalize: ${FINALIZE_STAGES[*]}
Control planes: $(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG") -> $(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
Workers: $(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG") -> $(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
VictoriaMetrics: $(profile_mode "$SOURCE_CONFIG") -> $(profile_mode "$TARGET_CONFIG")
Components retired only at finalize: $(components_to_remove | paste -sd, - || true)
External backup endpoint: ${DR_ENDPOINT:-MISSING}
External backup bucket: ${DR_BUCKET:-MISSING}
Non-shrinking PVC overrides: $(wc -l < "$STORAGE_RETENTION_FILE" | tr -d ' ')
Data-bearing replica overrides: $(wc -l < "$STATEFUL_RETENTION_FILE" | tr -d ' ')
Preserved component selection overrides: $(wc -l < "$SELECTION_RETENTION_FILE" | tr -d ' ')
Retained bastion server type: $(yq -r '.network.bastion.server_type // "disabled"' "$TARGET_CONFIG")
Estimated source persistent capacity: $(jq -r '.source.persistent_total_gib' "$CAPACITY_PLAN_FILE") GiB
Estimated target persistent capacity: $(jq -r '.target.persistent_total_gib' "$CAPACITY_PLAN_FILE") GiB
Estimated target-only/growth delta: $(jq -r '.target_delta_gib' "$CAPACITY_PLAN_FILE") GiB
Retained migration backup scratch: $(jq -r '.migration_scratch_gib' "$CAPACITY_PLAN_FILE") GiB
Required additional capacity before safety margin: $(jq -r '.required_additional_gib' "$CAPACITY_PLAN_FILE") GiB
Configured account volume quota: ${VOLUME_QUOTA_GIB:-MISSING} GiB
Configured safety margin: ${VOLUME_SAFETY_MARGIN_GIB} GiB
Offline capacity result: $(jq -r '.offline_result' "$CAPACITY_PLAN_FILE")
EOF
  [[ ! -s "$STORAGE_RETENTION_FILE" ]] || { printf '\nRetained storage requests (source, requested target, YAML path):\n'; sed 's/\t/  /g' "$STORAGE_RETENTION_FILE"; }
  [[ ! -s "$STATEFUL_RETENTION_FILE" ]] || { printf '\nRetained data-bearing replicas (source, requested target, YAML path):\n'; sed 's/\t/  /g' "$STATEFUL_RETENTION_FILE"; }
  [[ ! -s "$SELECTION_RETENTION_FILE" ]] || { printf '\nPreserved component selections (YAML path, source-profile default, active value):\n'; sed 's/\t/  /g' "$SELECTION_RETENTION_FILE"; }
  exit 0
fi

if [[ "$COMMAND" == execute ]]; then
  if [[ -f "$POINTER_FILE" ]]; then
    other_state_dir=$(<"$POINTER_FILE")
    if [[ "$other_state_dir" != "$STATE_DIR" && -f "$other_state_dir/state.json" ]]; then
      other_status=$(jq -r '.status' "$other_state_dir/state.json")
      case "$other_status" in
        finalized|rolled_back) ;;
        *) fail "another migration for $PROJECT is active with status=$other_status: $other_state_dir" ;;
      esac
    fi
  fi
  archive_reusable_state
  generate_configs
  validate_generated_configs
  if [[ "$DRY_RUN" == true ]]; then
    for stage in "${STAGES[@]}"; do dry "would run stage: $stage"; done
    exit 0
  fi
  validate_volume_capacity_settings
  [[ $(jq -r '.offline_result' "$CAPACITY_PLAN_FILE") != impossible-even-empty-account ]] \
    || fail "estimated migration capacity plus safety margin exceeds the configured account quota even with zero existing volumes"
  validate_operator_state_inputs
  if [[ "$FORCE" != true ]]; then
    printf 'Type MIGRATE to start %s -> %s migration for %s: ' "$SOURCE_PROFILE" "$TARGET_PROFILE" "$PROJECT"
    read -r confirmation; [[ "$confirmation" == MIGRATE ]] || fail "confirmation did not match MIGRATE"
  fi
  jq -n --slurpfile capacityPlan "$CAPACITY_PLAN_FILE" \
    --arg project "$PROJECT" --arg source "$SOURCE_PROFILE" --arg target "$TARGET_PROFILE" \
    --arg activeConfig "$CONFIG_FILE" --arg operatorStateRoot "$OPERATOR_STATE_ROOT" \
    --arg secretsFile "$SECRETS_FILE" --arg vaultInitFile "$VAULT_INIT_FILE" \
    --arg sshKeyPath "$SSH_KEY_PATH" --arg sshKnownHostsFile "$SSH_KNOWN_HOSTS_FILE" \
    --argjson k8sApiLocalPort "$K8S_API_LOCAL_PORT" \
    --argjson volumeQuota "$VOLUME_QUOTA_GIB" \
    --argjson volumeSafetyMargin "$VOLUME_SAFETY_MARGIN_GIB" \
    --argjson sourceCp "$(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG")" \
    --argjson sourceWorkers "$(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG")" \
    --argjson targetCp "$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")" \
    --argjson targetWorkers "$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")" \
    '{schema_version:4,project:$project,source_profile:$source,target_profile:$target,
      active_config:$activeConfig,status:"in_progress",
      created_at:(now | todateiso8601),last_completed_stage:null,
      operator_state:{root:$operatorStateRoot,secrets_file:$secretsFile,
        vault_init_file:$vaultInitFile,ssh_key_path:$sshKeyPath,
        ssh_known_hosts_file:$sshKnownHostsFile,
        k8s_api_local_port:$k8sApiLocalPort},
      volume_capacity:{quota_gib:$volumeQuota,safety_margin_gib:$volumeSafetyMargin,
        plan:$capacityPlan[0],status:"pending-live-check"},
      topology:{source:{control_planes:$sourceCp,workers:$sourceWorkers},target:{control_planes:$targetCp,workers:$targetWorkers}}}' > "$STATE_FILE"
  printf '%s\n' "$STATE_DIR" > "$POINTER_FILE"
elif [[ "$COMMAND" == resume ]]; then
  for generated in "$SOURCE_CONFIG" "$TARGET_CONFIG" "$STEADY_CONFIG" "$EXPANSION_CONFIG" "$BACKUP_CONFIG" "$ROLLBACK_CONFIG" "$CAPACITY_PLAN_FILE"; do
    [[ -f "$generated" ]] || fail "migration config is missing: $generated"
  done
  case $(jq -r '.status' "$STATE_FILE") in
    completed|finalized) log "migration has no pending execution checkpoints"; exit 0 ;;
    rolled_back) fail "migration was rolled back; start a new execute workflow" ;;
  esac
fi

if [[ "$DRY_RUN" != true ]]; then
  case "$COMMAND" in
    resume)
      validate_operator_state_inputs
      persist_controller_runtime_state
      validate_volume_capacity_settings
      ;;
    rollback|finalize)
      validate_operator_state_inputs
      persist_controller_runtime_state
      ;;
  esac
fi

run_playbook() {
  local config="$1"; shift
  ansible-playbook "$PROJECT_ROOT/playbooks/deploy_platform.yml" -e "@$config" \
    -e "project_name=$PROJECT" -e "domain=$DOMAIN" -e "email=$EMAIL" \
    -e "ssh_key_path=$SSH_KEY_PATH" -e "k8s_api_local_port=$K8S_API_LOCAL_PORT" \
    -e "secrets_file=$SECRETS_FILE" -e "vault_init_output_file=$VAULT_INIT_FILE" "$@"
}

persist_active_config() {
  local source="$1" destination
  local recorded=""
  [[ -f "$STATE_FILE" ]] && recorded=$(jq -r '.active_config // ""' "$STATE_FILE")
  [[ -n "$recorded" ]] || recorded="$PROJECT_ROOT/platform-orchestrator/platform.yaml"
  for destination in "$CONFIG_FILE" "$recorded"; do
    [[ "$destination" != "$source" ]] || continue
    cmp -s "$source" "$destination" || cp "$source" "$destination"
  done
}

check_platform_health() {
  local config="$1" require_argocd require_postgresql require_mongodb workload_failures helm_failures cert_json not_ready_certificates pg_state mongo_state state_lower not_bound
  require_argocd=$(yq -r '.gitops.enabled // false' "$config")
  require_postgresql=$(yq -r '(.databases.enabled and .databases.postgresql.enabled) // false' "$config")
  require_mongodb=$(yq -r '(.databases.enabled and .databases.mongodb.enabled) // false' "$config")
  if ! HEALTH_REQUIRE_ARGOCD="$require_argocd" \
    HEALTH_REQUIRE_POSTGRESQL="$require_postgresql" \
    HEALTH_REQUIRE_MONGODB="$require_mongodb" \
    "$SCRIPT_DIR/health-gates.sh"; then
    return 1
  fi
  workload_failures=$(kubectl get deployments,statefulsets,daemonsets -A -o json | jq '[.items[] | select(
    (.kind == "Deployment" and ((.status.readyReplicas // 0) < (.spec.replicas // 1) or (.status.updatedReplicas // 0) < (.spec.replicas // 1))) or
    (.kind == "StatefulSet" and ((.status.readyReplicas // 0) < (.spec.replicas // 1) or (((.spec.updateStrategy.type // "RollingUpdate") != "OnDelete") and (.status.updatedReplicas // 0) < (.spec.replicas // 1)))) or
    (.kind == "DaemonSet" and ((.status.numberReady // 0) < (.status.desiredNumberScheduled // 0) or (((.spec.updateStrategy.type // "RollingUpdate") != "OnDelete") and (.status.updatedNumberScheduled // 0) < (.status.desiredNumberScheduled // 0))))) ] | length')
  [[ "$workload_failures" == 0 ]] || fail "$workload_failures controller workloads are not fully rolled out"
  helm_failures=$(helm list --all-namespaces --output json | jq '[.[] | select(.status != "deployed")] | length')
  [[ "$helm_failures" == 0 ]] || fail "$helm_failures active Helm releases are not deployed"
  if cert_json=$(kubectl get certificates --all-namespaces -o json 2>/dev/null); then
    not_ready_certificates=$(jq '[.items[] | select([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 0)] | length' <<<"$cert_json")
    [[ "$not_ready_certificates" == 0 ]] || fail "$not_ready_certificates TLS certificates are not Ready"
  fi
  if [[ "$require_postgresql" == true ]]; then
    pg_state=$(kubectl get perconapgcluster "${PROJECT}-pg" -n databases -o jsonpath='{.status.state}')
    state_lower=$(printf '%s' "$pg_state" | tr '[:upper:]' '[:lower:]')
    [[ "$state_lower" == ready ]] || fail "PostgreSQL operator state is ${pg_state:-missing}"
  fi
  if [[ "$require_mongodb" == true ]]; then
    mongo_state=$(kubectl get perconaservermongodb "${PROJECT}-mongo" -n databases -o jsonpath='{.status.state}')
    state_lower=$(printf '%s' "$mongo_state" | tr '[:upper:]' '[:lower:]')
    [[ "$state_lower" == ready ]] || fail "MongoDB operator state is ${mongo_state:-missing}"
  fi
  not_bound=$(kubectl get pvc -A -o json | jq '[.items[] | select(.status.phase != "Bound")] | length')
  [[ "$not_bound" == 0 ]] || fail "$not_bound PVCs are not Bound"
}

cleanup_probe_pod() {
  kubectl delete pod -n "$1" "$2" --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1 || true
}

cleanup_stale_data_path_probes() {
  local namespace
  for namespace in storage monitoring; do
    kubectl delete pod -n "$namespace" \
      -l 'app.kubernetes.io/part-of=profile-migration,app.kubernetes.io/component=data-path-probe' \
      --ignore-not-found --wait=true --timeout=60s >/dev/null 2>&1 || true
  done
}

# Controller readiness alone is insufficient for stateful services: Loki can
# have a present Service and PVC while its store initialization is crashing,
# and SeaweedFS can be Ready while its S3 data path is unusable. Exercise the
# exact source data paths before and after every one-node drain. Probe objects
# are unique, content-verified, deleted by the pod, and defensively removed by
# the controller even when the probe fails.
check_stateful_data_paths() {
  local config="$1" pod manifest output
  if [[ $(yq -r '(.storage.enabled // false)' "$config") == true ]]; then
    pod="profile-migration-s3-probe-$$-${RANDOM}"
    manifest=$(jq -n --arg name "$pod" '{
      apiVersion:"v1", kind:"Pod",
      metadata:{name:$name,namespace:"storage",labels:{"app.kubernetes.io/part-of":"profile-migration","app.kubernetes.io/component":"data-path-probe"}},
      spec:{restartPolicy:"Never",automountServiceAccountToken:false,
        securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,seccompProfile:{type:"RuntimeDefault"}},
        containers:[{name:"aws-cli",image:"amazon/aws-cli:2.34.48",
          command:["/bin/sh","-ec"],
          args:["set -eu; key=s3://backups/profile-migration-health/$HOSTNAME; cleanup() { aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 rm \"$key\" >/dev/null 2>&1 || true; }; trap cleanup EXIT; printf %s \"$HOSTNAME\" >/tmp/value; aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 cp /tmp/value \"$key\" >/dev/null; aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 cp \"$key\" /tmp/read >/dev/null; test \"$(cat /tmp/read)\" = \"$HOSTNAME\"; cleanup; trap - EXIT"],
          envFrom:[{secretRef:{name:"seaweedfs-s3-config"}}],
          env:[{name:"AWS_DEFAULT_REGION",value:"us-east-1"},{name:"AWS_EC2_METADATA_DISABLED",value:"true"}],
          resources:{requests:{cpu:"25m",memory:"32Mi"},limits:{cpu:"250m",memory:"128Mi"}},
          securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]},readOnlyRootFilesystem:false}
        }]}}
    ')
    cleanup_probe_pod storage "$pod"
    kubectl create -f - <<<"$manifest" >/dev/null
    if ! kubectl wait -n storage "pod/${pod}" --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null; then
      output=$(kubectl logs -n storage "$pod" --tail=100 2>&1 || true)
      cleanup_probe_pod storage "$pod"
      printf '%s\n' "$output" >&2
      fail "SeaweedFS S3 write/read/delete probe failed"
    fi
    cleanup_probe_pod storage "$pod"
  fi

  if [[ $(yq -r '(.observability.enabled // false) and ((.observability.logging.stack // "loki") == "loki")' "$config") == true ]]; then
    pod="profile-migration-loki-probe-$$-${RANDOM}"
    manifest=$(jq -n --arg name "$pod" '{
      apiVersion:"v1", kind:"Pod",
      metadata:{name:$name,namespace:"monitoring",labels:{"app.kubernetes.io/part-of":"profile-migration","app.kubernetes.io/component":"data-path-probe"}},
      spec:{restartPolicy:"Never",automountServiceAccountToken:false,
        securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,seccompProfile:{type:"RuntimeDefault"}},
        containers:[{name:"curl",image:"curlimages/curl:8.17.0",
          command:["/bin/sh","-ec"],
          args:["set -eu; now=$(date +%s); marker=$HOSTNAME-$now; ts=${now}000000000; start=$((now - 5))000000000; end=$((now + 30))000000000; payload=$(printf \"{\\\"streams\\\":[{\\\"stream\\\":{\\\"job\\\":\\\"profile-migration-probe\\\",\\\"marker\\\":\\\"%s\\\"},\\\"values\\\":[[\\\"%s\\\",\\\"%s\\\"]]}]}\" \"$marker\" \"$ts\" \"$marker\"); curl -kfsS --retry 6 --retry-delay 5 --max-time 30 -H \"X-Scope-OrgID: fake\" -H \"Content-Type: application/json\" -X POST http://loki-gateway.monitoring.svc/loki/api/v1/push --data \"$payload\"; sleep 5; response=$(curl -kfsS --retry 6 --retry-delay 5 --max-time 30 -H \"X-Scope-OrgID: fake\" --get http://loki-gateway.monitoring.svc/loki/api/v1/query_range --data-urlencode \"query={job=\\\"profile-migration-probe\\\",marker=\\\"$marker\\\"}\" --data-urlencode \"start=$start\" --data-urlencode \"end=$end\" --data-urlencode \"limit=10\"); printf %s \"$response\" | grep -F \"$marker\" >/dev/null"],
          resources:{requests:{cpu:"10m",memory:"16Mi"},limits:{cpu:"100m",memory:"64Mi"}},
          securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]},readOnlyRootFilesystem:true}
        }]}}
    ')
    cleanup_probe_pod monitoring "$pod"
    kubectl create -f - <<<"$manifest" >/dev/null
    if ! kubectl wait -n monitoring "pod/${pod}" --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null; then
      output=$(kubectl logs -n monitoring "$pod" --tail=100 2>&1 || true)
      cleanup_probe_pod monitoring "$pod"
      printf '%s\n' "$output" >&2
      fail "Loki fresh push/query probe failed"
    fi
    cleanup_probe_pod monitoring "$pod"
  fi
}

wait_for_platform_convergence() {
  local config="$1" label="$2" deadline=$((SECONDS + PLATFORM_CONVERGENCE_TIMEOUT))
  local attempt=0 output=""
  while (( SECONDS < deadline )); do
    attempt=$((attempt + 1))
    # A controller crash can leave an earlier short-lived probe behind. Remove
    # only our exact probe label before the all-pod health gate so resume does
    # not deadlock on its own stale evidence.
    cleanup_stale_data_path_probes
    if output=$( (check_platform_health "$config" && check_stateful_data_paths "$config") 2>&1); then
      printf '%s\n' "$output"
      log "$label passed after $attempt attempt(s)"
      return 0
    fi
    warn "$label is not converged (attempt $attempt); retrying"
    if (( attempt == 1 || attempt % 4 == 0 )); then
      printf '%s\n' "$output" | tail -40 >&2
    fi
    sleep "$PLATFORM_CONVERGENCE_INTERVAL"
  done
  printf '%s\n' "$output" | tail -80 >&2
  fail "$label did not converge within ${PLATFORM_CONVERGENCE_TIMEOUT}s"
}

ssh_args_for_facts() {
  local facts="${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml" quoted_identity quoted_known_hosts proxy_command
  [[ -f "$facts" ]] || fail "infrastructure facts are missing: $facts"
  BASTION=$(yq -r '.bastion_public_ip' "$facts")
  [[ -f "$SSH_KEY_PATH" ]] || fail "SSH identity is missing: ${SSH_KEY_PATH:-not configured}"
  [[ -f "$SSH_KNOWN_HOSTS_FILE" ]] || fail "project SSH known-hosts file is missing: $SSH_KNOWN_HOSTS_FILE"
  printf -v quoted_identity '%q' "$SSH_KEY_PATH"
  printf -v quoted_known_hosts '%q' "$SSH_KNOWN_HOSTS_FILE"
  proxy_command="ssh -i ${quoted_identity} -o IdentitiesOnly=yes -o BatchMode=yes -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile=${quoted_known_hosts} -o ConnectTimeout=20 -W %h:%p root@${BASTION}"
  SSH_ARGS=(-i "$SSH_KEY_PATH" -o IdentitiesOnly=yes -o BatchMode=yes \
    -o StrictHostKeyChecking=accept-new -o "UserKnownHostsFile=${SSH_KNOWN_HOSTS_FILE}" \
    -o ConnectTimeout=20 -o "ProxyCommand=${proxy_command}")
}

check_etcd_health() {
  local excluded_node="${1:-}" facts="${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml" host output
  local deadline=$((SECONDS + ETCD_HEALTH_TIMEOUT)) attempt=0
  ssh_args_for_facts
  if [[ -n "$excluded_node" ]]; then
    host=$(EXCLUDED_NODE="$excluded_node" yq -r '.master_ips | to_entries | map(select(.key != strenv(EXCLUDED_NODE))) | .[0].value' "$facts")
  else
    host=$(yq -r '.master_ips | to_entries | .[0].value' "$facts")
  fi
  [[ -n "$host" && "$host" != null ]] || fail "no healthy etcd peer is available for verification"
  while (( SECONDS < deadline )); do
    # Never let ssh consume the stdin that may be feeding an enclosing
    # process-substitution loop (for example the control-plane node list).
    if output=$(ssh "${SSH_ARGS[@]}" "root@${host}" 'set -eu; . /etc/etcd.env; export ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl endpoint health --cluster' </dev/null 2>&1); then
      printf '%s\n' "$output"
      return 0
    fi
    attempt=$((attempt + 1))
    warn "etcd quorum is not fully ready yet (attempt $attempt); retrying"
    sleep 10
  done
  printf '%s\n' "$output" >&2
  fail "etcd cluster did not become healthy within ${ETCD_HEALTH_TIMEOUT}s"
}

control_plane_private_ip() {
  local node="$1" facts="${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml"
  NODE="$node" yq -r '.master_ips[strenv(NODE)] // ""' "$facts"
}

control_plane_etcd_endpoints() {
  local config="$1" count i ip
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  for ((i=1; i<=count; i++)); do
    ip=$(control_plane_private_ip "${PROJECT}-master-${i}")
    [[ -n "$ip" ]] || fail "private IP is missing for ${PROJECT}-master-${i}"
    printf 'https://%s:2379\n' "$ip"
  done
}

control_plane_etcd_endpoint_csv() {
  local config="$1" count i ip output="" endpoint
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  for ((i=1; i<=count; i++)); do
    ip=$(control_plane_private_ip "${PROJECT}-master-${i}")
    [[ -n "$ip" ]] || fail "private IP is missing for ${PROJECT}-master-${i}"
    endpoint="https://${ip}:2379"
    [[ -z "$output" ]] || output+=,
    output+="$endpoint"
  done
  printf '%s\n' "$output"
}

check_etcd_member_contract() {
  local config="$1" count host endpoints output actual expected_members actual_members learners i ip
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  host=$(control_plane_private_ip "${PROJECT}-master-1")
  endpoints=$(control_plane_etcd_endpoint_csv "$config")
  ssh_args_for_facts
  # The inventory-derived endpoint CSV must be expanded by the controller and
  # injected into the remote etcdctl environment.
  # shellcheck disable=SC2029
  output=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
    "set -eu; set -a; . /etc/etcd.env; set +a; export ETCDCTL_ENDPOINTS=${endpoints}; etcdctl endpoint health >/dev/null; etcdctl endpoint status >/dev/null; test -z \"\$(etcdctl alarm list)\"; etcdctl member list -w json" </dev/null) \
    || fail "could not verify every expected etcd endpoint"
  actual=$(jq '.members | length' <<<"$output")
  learners=$(jq '[.members[] | select(.isLearner == true)] | length' <<<"$output")
  expected_members=""
  for ((i=1; i<=count; i++)); do
    ip=$(control_plane_private_ip "${PROJECT}-master-${i}")
    [[ -n "$ip" ]] || fail "private IP is missing for ${PROJECT}-master-${i}"
    expected_members+="etcd${i} https://${ip}:2380 https://${ip}:2379"$'\n'
  done
  expected_members=$(printf '%s' "$expected_members" | sort)
  actual_members=$(jq -r '.members[] | "\(.name) \(.peerURLs | sort | join(",")) \(.clientURLs | sort | join(","))"' <<<"$output" | sort)
  [[ "$actual" == "$count" && "$learners" == 0 ]] \
    || fail "etcd membership mismatch: expected=${count} voting members actual=${actual} learners=${learners}"
  [[ "$actual_members" == "$expected_members" ]] \
    || fail "etcd member names, peer URLs, or client URLs do not match the control-plane inventory"
}

control_plane_etcd_clients_match() {
  local config="$1" count endpoints expected_config actual_config actual_http i node host
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  endpoints=$(control_plane_etcd_endpoint_csv "$config")
  expected_config=$(control_plane_etcd_endpoints "$config" | sort)
  actual_config=$(kubectl -n kube-system get configmap kubeadm-config -o json \
    | jq -r '.data.ClusterConfiguration' | yq -r '.etcd.external.endpoints[]' | sort) \
    || return 1
  actual_http=$(kubectl -n kube-system get configmap kubeadm-config -o json \
    | jq -r '.data.ClusterConfiguration' | yq -r '.etcd.external.httpEndpoints[]' | sort) \
    || return 1
  [[ "$actual_config" == "$expected_config" ]] || return 1
  [[ "$actual_http" == "$expected_config" ]] || return 1
  ssh_args_for_facts
  for ((i=1; i<=count; i++)); do
    node="${PROJECT}-master-${i}"
    host=$(control_plane_private_ip "$node")
    # These inventory-derived values intentionally expand on the controller;
    # the remote command validates the exact generated manifest arguments.
    # shellcheck disable=SC2029
    ssh "${SSH_ARGS[@]}" "root@${host}" \
      "set -eu; systemctl is-active --quiet etcd; grep -Fq -- '--etcd-servers=${endpoints}' /etc/kubernetes/manifests/kube-apiserver.yaml; grep -Fq -- '--etcd-certfile=/etc/ssl/etcd/ssl/node-${node}.pem' /etc/kubernetes/manifests/kube-apiserver.yaml; grep -Fq -- '--etcd-keyfile=/etc/ssl/etcd/ssl/node-${node}-key.pem' /etc/kubernetes/manifests/kube-apiserver.yaml; KUBECONFIG=/etc/kubernetes/admin.conf kubectl --server=https://127.0.0.1:6443 --request-timeout=8s get --raw=/readyz >/dev/null" </dev/null \
      || return 1
  done
}

reconcile_control_plane_etcd_clients() {
  local config="$1" count endpoints i node host old_id new_id attempt
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  endpoints=$(control_plane_etcd_endpoint_csv "$config")
  ssh_args_for_facts
  host=$(control_plane_private_ip "${PROJECT}-master-1")
  ssh "${SSH_ARGS[@]}" "root@${host}" \
    'set -eu; kubeadm init phase upload-config kubeadm --config /etc/kubernetes/kubeadm-config.yaml' </dev/null \
    || fail "could not upload the expanded kubeadm cluster configuration"
  # Reconcile secondary API servers first so the original endpoint remains
  # available until two independent replacements have passed local readiness.
  for ((i=2; i<=count; i++)); do
    node="${PROJECT}-master-${i}"
    host=$(control_plane_private_ip "$node")
    old_id=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
      'crictl ps -q --name kube-apiserver | head -1' </dev/null)
    ssh "${SSH_ARGS[@]}" "root@${host}" \
      "set -eu; cp -a /etc/kubernetes/manifests/kube-apiserver.yaml /root/kube-apiserver.yaml.pre-etcd-ha; kubeadm init phase control-plane apiserver --config /etc/kubernetes/kubeadm-config.yaml >/dev/null" </dev/null \
      || fail "could not reconcile kube-apiserver etcd endpoints on $node"
    for attempt in {1..60}; do
      new_id=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
        'crictl ps -q --name kube-apiserver | head -1' </dev/null 2>/dev/null || true)
      # shellcheck disable=SC2029
      if [[ -n "$new_id" && "$new_id" != "$old_id" ]] && ssh "${SSH_ARGS[@]}" "root@${host}" \
        "grep -Fq -- '--etcd-servers=${endpoints}' /etc/kubernetes/manifests/kube-apiserver.yaml && KUBECONFIG=/etc/kubernetes/admin.conf kubectl --server=https://127.0.0.1:6443 --request-timeout=8s get --raw=/readyz >/dev/null" </dev/null; then
        break
      fi
      sleep 5
    done
    [[ -n "$new_id" && "$new_id" != "$old_id" ]] \
      || fail "$node kube-apiserver did not restart after etcd endpoint reconciliation"
  done
  node="${PROJECT}-master-1"
  host=$(control_plane_private_ip "$node")
  old_id=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
    'crictl ps -q --name kube-apiserver | head -1' </dev/null)
  ssh "${SSH_ARGS[@]}" "root@${host}" \
    "set -eu; cp -a /etc/kubernetes/manifests/kube-apiserver.yaml /root/kube-apiserver.yaml.pre-etcd-ha; kubeadm init phase control-plane apiserver --config /etc/kubernetes/kubeadm-config.yaml >/dev/null" </dev/null \
    || fail "could not reconcile kube-apiserver etcd endpoints on $node"
  for attempt in {1..60}; do
    new_id=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
      'crictl ps -q --name kube-apiserver | head -1' </dev/null 2>/dev/null || true)
    # shellcheck disable=SC2029
    if [[ -n "$new_id" && "$new_id" != "$old_id" ]] && ssh "${SSH_ARGS[@]}" "root@${host}" \
      "grep -Fq -- '--etcd-servers=${endpoints}' /etc/kubernetes/manifests/kube-apiserver.yaml && KUBECONFIG=/etc/kubernetes/admin.conf kubectl --server=https://127.0.0.1:6443 --request-timeout=8s get --raw=/readyz >/dev/null" </dev/null; then
      break
    fi
    sleep 5
  done
  [[ -n "$new_id" && "$new_id" != "$old_id" ]] \
    || fail "$node kube-apiserver did not restart after etcd endpoint reconciliation"
  wait_for_api_ready
}

ensure_control_plane_etcd_ha() {
  local config="$1"
  check_etcd_member_contract "$config"
  if ! control_plane_etcd_clients_match "$config"; then
    warn "control-plane etcd client endpoints are stale; reconciling kubeadm and API server manifests"
    reconcile_control_plane_etcd_clients "$config"
  fi
  control_plane_etcd_clients_match "$config" \
    || fail "control-plane API servers do not all use the complete etcd endpoint set"
  check_etcd_member_contract "$config"
}

check_control_plane_alternates_ready() {
  local excluded_node="$1" config="$2" count i node host alternates=0
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  (( count >= 3 )) || return 0
  ssh_args_for_facts
  for ((i=1; i<=count; i++)); do
    node="${PROJECT}-master-${i}"
    [[ "$node" != "$excluded_node" ]] || continue
    host=$(control_plane_private_ip "$node")
    ssh "${SSH_ARGS[@]}" "root@${host}" \
      'set -eu; systemctl is-active --quiet etcd; set -a; . /etc/etcd.env; set +a; etcdctl endpoint health >/dev/null; KUBECONFIG=/etc/kubernetes/admin.conf kubectl --server=https://127.0.0.1:6443 --request-timeout=8s get --raw=/readyz >/dev/null' </dev/null \
      || fail "alternate control-plane endpoint $node is not ready before draining $excluded_node"
    alternates=$((alternates + 1))
  done
  (( alternates >= 2 )) || fail "fewer than two alternate control-plane endpoints are ready"
}

check_control_plane_survivors() {
  local excluded_node="$1" config="$2" count i node host survivor_host="" endpoints="" endpoint survivor_count=0
  count=$(yq -r '.infrastructure.control_plane.count' "$config")
  (( count >= 3 )) \
    || fail "cannot safely resize a control-plane node without at least three expanded control-plane members"
  ssh_args_for_facts
  for ((i=1; i<=count; i++)); do
    node="${PROJECT}-master-${i}"
    [[ "$node" != "$excluded_node" ]] || continue
    host=$(control_plane_private_ip "$node")
    [[ -n "$survivor_host" ]] || survivor_host="$host"
    endpoint="https://${host}:2379"
    [[ -z "$endpoints" ]] || endpoints+=,
    endpoints+="$endpoint"
    survivor_count=$((survivor_count + 1))
    ssh "${SSH_ARGS[@]}" "root@${host}" \
      'set -eu; systemctl is-active --quiet etcd; KUBECONFIG=/etc/kubernetes/admin.conf kubectl --server=https://127.0.0.1:6443 --request-timeout=8s get --raw=/readyz >/dev/null' </dev/null \
      || fail "surviving control-plane endpoint $node is not independently ready"
  done
  (( survivor_count >= 2 )) || fail "fewer than two control-plane survivors remain"
  # The survivor-only endpoint CSV is built locally from the replacement
  # inventory and intentionally expanded into the remote etcdctl environment.
  # shellcheck disable=SC2029
  ssh "${SSH_ARGS[@]}" "root@${survivor_host}" \
    "set -eu; set -a; . /etc/etcd.env; set +a; export ETCDCTL_ENDPOINTS=${endpoints}; etcdctl endpoint health >/dev/null" </dev/null \
    || fail "surviving etcd members cannot commit without $excluded_node"
  wait_for_api_ready
}

wait_for_csi_detach() {
  local node="$1" deadline=$((SECONDS + CSI_DETACH_TIMEOUT)) attachments provider_volumes
  while (( SECONDS < deadline )); do
    attachments=$(kubectl get volumeattachments.storage.k8s.io -o json \
      | jq --arg node "$node" '[.items[] | select(.spec.nodeName == $node)] | length')
    provider_volumes=$(hcloud server describe "$node" -o json | jq '.volumes | length')
    if [[ "$attachments" == 0 && "$provider_volumes" == 0 ]]; then return 0; fi
    warn "$attachments CSI attachment object(s) and $provider_volumes provider volume(s) remain on drained node $node; waiting for detach"
    sleep 10
  done
  fail "Kubernetes and provider volumes did not fully detach from $node within ${CSI_DETACH_TIMEOUT}s"
}

run_with_timeout() {
  local timeout_seconds="$1" command_pid timer_pid status
  shift
  "$@" &
  command_pid=$!
  (
    sleep "$timeout_seconds"
    kill -TERM "$command_pid" >/dev/null 2>&1 || exit 0
    sleep 5
    kill -KILL "$command_pid" >/dev/null 2>&1 || true
  ) &
  timer_pid=$!
  if wait "$command_pid"; then status=0; else status=$?; fi
  kill "$timer_pid" >/dev/null 2>&1 || true
  wait "$timer_pid" 2>/dev/null || true
  return "$status"
}

retry_gate() {
  local label="$1" attempt
  shift
  for attempt in 1 2 3 4 5 6; do
    if ( "$@" ); then
      return 0
    fi
    warn "$label failed on attempt $attempt; retrying"
    sleep $((attempt * 2))
  done
  fail "$label failed after retries"
}

server_status() {
  hcloud server describe "$1" -o json | jq -r '.status'
}

wait_for_server_settled() {
  local node="$1" status deadline=$((SECONDS + HCLOUD_STATE_TIMEOUT))
  while (( SECONDS < deadline )); do
    status=$(server_status "$node" 2>/dev/null || echo unknown)
    case "$status" in
      running|off) printf '%s\n' "$status"; return 0 ;;
    esac
    sleep 15
  done
  fail "Hetzner server $node did not reach a stable running/off state within ${HCLOUD_STATE_TIMEOUT}s"
}

ensure_server_stopped() {
  local node="$1" status deadline
  status=$(wait_for_server_settled "$node")
  if [[ "$status" == running ]]; then
    if ! run_with_timeout "$HCLOUD_CLIENT_TIMEOUT" hcloud server poweroff "$node"; then
      status=$(server_status "$node" 2>/dev/null || echo unknown)
      [[ "$status" == stopping || "$status" == off ]] \
        || fail "failed to power off $node (provider status: $status)"
      warn "Hetzner accepted the power-off for $node but the local CLI did not finish cleanly; continuing from provider state"
    fi
  fi
  deadline=$((SECONDS + HCLOUD_STATE_TIMEOUT))
  while (( SECONDS < deadline )); do
    [[ $(server_status "$node" 2>/dev/null || echo unknown) == off ]] && return 0
    sleep 15
  done
  status=$(server_status "$node" 2>/dev/null || echo unknown)
  fail "Hetzner server $node did not become off within ${HCLOUD_STATE_TIMEOUT}s (provider status: $status)"
}

server_type_available_for_node() {
  local node="$1" server_type="$2" location
  location=$(hcloud server describe "$node" -o json | jq -r '.location.name // ""')
  [[ -n "$location" ]] || return 1
  hcloud server-type describe "$server_type" -o json \
    | jq -e --arg location "$location" 'any(.locations[]?; .name == $location and .available == true)' >/dev/null
}

record_node_type_override() {
  local node="$1" requested="$2" fallback="$3" tmp config
  for config in "$TARGET_CONFIG" "$STEADY_CONFIG" "$POST_BACKUP_CONFIG"; do
    [[ -f "$config" ]] || continue
    NODE="$node" FALLBACK="$fallback" yq -i \
      '.infrastructure.node_type_overrides[strenv(NODE)] = strenv(FALLBACK)' "$config"
  done
  mkdir -p "$(dirname "$NODE_TYPE_OVERRIDE_FILE")"
  tmp="${NODE_TYPE_OVERRIDE_FILE}.tmp.$$"
  { [[ ! -f "$NODE_TYPE_OVERRIDE_FILE" ]] || awk -F '\t' -v node="$node" '$1 != node' "$NODE_TYPE_OVERRIDE_FILE"; \
    printf '%s\t%s\t%s\tequivalent-capacity-fallback\n' "$node" "$requested" "$fallback"; } > "$tmp"
  mv "$tmp" "$NODE_TYPE_OVERRIDE_FILE"
  tmp="${STATE_FILE}.tmp.$$"
  jq --arg node "$node" --arg requested "$requested" --arg fallback "$fallback" '
    .node_type_overrides[$node]={requested:$requested,active:$fallback,
      reason:"equivalent-capacity-fallback",recorded_at:(now | todateiso8601)} |
    .updated_at=(now | todateiso8601)
  ' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

select_equivalent_fallback_type() {
  local node="$1" requested="$2" candidate requested_json candidate_json
  local requested_cores requested_memory requested_arch requested_cpu
  requested_json=$(hcloud server-type describe "$requested" -o json)
  requested_cores=$(jq -r '.cores' <<<"$requested_json")
  requested_memory=$(jq -r '.memory' <<<"$requested_json")
  requested_arch=$(jq -r '.architecture' <<<"$requested_json")
  requested_cpu=$(jq -r '.cpu_type' <<<"$requested_json")
  for candidate in ${HCLOUD_EQUIVALENT_FALLBACK_TYPES//,/ }; do
    [[ -n "$candidate" && "$candidate" != "$requested" ]] || continue
    candidate_json=$(hcloud server-type describe "$candidate" -o json 2>/dev/null || true)
    [[ -n "$candidate_json" ]] || continue
    [[ $(jq -r '.cores' <<<"$candidate_json") == "$requested_cores" ]] || continue
    [[ $(jq -r '.memory' <<<"$candidate_json") == "$requested_memory" ]] || continue
    [[ $(jq -r '.architecture' <<<"$candidate_json") == "$requested_arch" ]] || continue
    [[ $(jq -r '.cpu_type' <<<"$candidate_json") == "$requested_cpu" ]] || continue
    server_type_available_for_node "$node" "$candidate" || continue
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

change_server_type_with_retry() {
  local node="$1" target_type="$2" target_disk="$3" keep_disk="${4:-false}"
  local attempt server_json current_type current_disk delay
  local -a change_args=(server change-type "$node" "$target_type")
  [[ "$keep_disk" != true ]] || change_args+=(--keep-disk)
  for ((attempt=1; attempt<=HCLOUD_CAPACITY_RETRY_ATTEMPTS; attempt++)); do
    # Capacity placement can fail transiently, and a failed action may leave the
    # server in a different power state. Re-establish the provider-side off gate
    # before every retry and trust only the authoritative post-action shape.
    ensure_server_stopped "$node"
    run_with_timeout "$HCLOUD_CLIENT_TIMEOUT" hcloud "${change_args[@]}" || true
    server_json=$(hcloud server describe "$node" -o json)
    current_type=$(jq -r '.server_type.name' <<<"$server_json")
    current_disk=$(jq -r '.primary_disk_size' <<<"$server_json")
    if [[ "$current_type" == "$target_type" ]] && (( current_disk >= target_disk )); then
      (( attempt == 1 )) || log "$node type change converged after $attempt provider attempts"
      return 0
    fi
    if (( attempt < HCLOUD_CAPACITY_RETRY_ATTEMPTS )); then
      delay=$((HCLOUD_CAPACITY_RETRY_INTERVAL * attempt))
      (( delay > 60 )) && delay=60
      warn "Hetzner type change for $node is not converged (attempt $attempt/$HCLOUD_CAPACITY_RETRY_ATTEMPTS); retrying in ${delay}s"
      sleep "$delay"
    fi
  done
  fail "failed to resize $node to type=$target_type disk>=${target_disk}GB after $HCLOUD_CAPACITY_RETRY_ATTEMPTS provider attempts"
}

ensure_server_running() {
  local node="$1" status deadline
  status=$(wait_for_server_settled "$node")
  if [[ "$status" == off ]]; then
    if ! run_with_timeout "$HCLOUD_CLIENT_TIMEOUT" hcloud server poweron "$node"; then
      status=$(server_status "$node" 2>/dev/null || echo unknown)
      [[ "$status" == starting || "$status" == running ]] \
        || fail "failed to power on $node (provider status: $status)"
      warn "Hetzner accepted the power-on for $node but the local CLI did not finish cleanly; continuing from provider state"
    fi
  fi
  deadline=$((SECONDS + HCLOUD_STATE_TIMEOUT))
  while (( SECONDS < deadline )); do
    [[ $(server_status "$node" 2>/dev/null || echo unknown) == running ]] && return 0
    sleep 15
  done
  fail "Hetzner server $node did not become running within ${HCLOUD_STATE_TIMEOUT}s"
}

mark_stage() {
  local stage="$1" tmp="${STATE_FILE}.tmp.$$"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/stage-${stage}.done"
  jq --arg stage "$stage" '.last_completed_stage=$stage | .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

mark_finalize_stage() {
  local stage="$1" tmp="${STATE_FILE}.tmp.$$"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/finalize-${stage}.done"
  jq --arg stage "$stage" '.last_completed_finalize_stage=$stage | .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
}

cluster_backup() {
  local config="$1"
  local args=(--config "$config" --secrets-file "$SECRETS_FILE" \
    --vault-init-file "$VAULT_INIT_FILE" \
    --ssh-identity "$SSH_KEY_PATH" --ssh-known-hosts "$SSH_KNOWN_HOSTS_FILE" \
    --output-dir "$STATE_DIR/backups" --force)
  [[ -z "$BACKUP_RECIPIENT" ]] || args+=(--recipient "$BACKUP_RECIPIENT")
  "$SCRIPT_DIR/cluster-backup.sh" "${args[@]}" \
    || fail "encrypted cluster backup gate failed; migration checkpoint was not advanced"
}

capture_live_bastion_type() {
  local server_json live_type source_declared target_requested config tmp
  local -a generated_configs=(
    "$SOURCE_CONFIG" "$TARGET_CONFIG" "$STEADY_CONFIG" "$EXPANSION_CONFIG"
    "$BACKUP_CONFIG" "$ROLLBACK_CONFIG" "$POST_BACKUP_CONFIG"
  )
  [[ $(yq -r '.network.bastion.enabled // false' "$SOURCE_CONFIG") == true ]] || return 0
  server_json=$(hcloud server describe "${PROJECT}-bastion" -o json) \
    || fail "could not read the retained bastion before migration"
  live_type=$(jq -r '.server_type.name // ""' <<<"$server_json")
  [[ -n "$live_type" ]] || fail "Hetzner returned no server type for ${PROJECT}-bastion"
  source_declared=$(yq -r '.network.bastion.server_type // ""' "$SOURCE_CONFIG")
  target_requested=$(jq -r '.bastion.target_requested_type // ""' "$STATE_FILE")
  if [[ -z "$target_requested" && -s "$BASTION_TYPE_RETENTION_FILE" ]]; then
    target_requested=$(awk -F '\t' 'NR == 1 && $3 == "target-requested" {print $4}' \
      "$BASTION_TYPE_RETENTION_FILE")
  fi
  [[ -n "$target_requested" ]] \
    || target_requested=$(yq -r '.network.bastion.server_type // ""' "$TARGET_CONFIG")
  for config in "${generated_configs[@]}"; do
    [[ -f "$config" ]] || continue
    set_yaml_string "$config" '.network.bastion.server_type' "$live_type"
  done
  printf 'source-declared\t%s\ttarget-requested\t%s\tlive-retained\t%s\n' \
    "$source_declared" "$target_requested" "$live_type" > "$BASTION_TYPE_RETENTION_FILE"
  tmp="${STATE_FILE}.tmp.$$"
  jq --arg server "${PROJECT}-bastion" --arg sourceDeclared "$source_declared" \
    --arg targetRequested "$target_requested" --arg retained "$live_type" '
      .bastion={server:$server,source_declared_type:$sourceDeclared,
        target_requested_type:$targetRequested,retained_type:$retained,
        resize_supported:false,captured_at:(.bastion.captured_at // (now | todateiso8601)),
        last_verified_at:(now | todateiso8601)} |
      .updated_at=(now | todateiso8601)
    ' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
  if ! [[ "$live_type" == "$source_declared" && "$live_type" == "$target_requested" ]]; then
    warn "retaining live bastion type $live_type; profile migration has no bastion resize path (declared source=$source_declared, requested target=$target_requested)"
  fi
}

preserve_non_shrinking_node_types() {
  local role path node server_json target_type_json current_type target_type current_disk target_disk
  local current_cores target_cores current_memory target_memory
  : > "$NODE_TYPE_RETENTION_FILE"
  for role in control_plane workers; do
    path=".infrastructure.${role}.type"
    if [[ "$role" == control_plane ]]; then node="${PROJECT}-master-1"; else node="${PROJECT}-worker-1"; fi
    server_json=$(hcloud server describe "$node" -o json)
    current_type=$(jq -r '.server_type.name' <<<"$server_json")
    current_disk=$(jq -r '.primary_disk_size' <<<"$server_json")
    current_cores=$(jq -r '.server_type.cores' <<<"$server_json")
    current_memory=$(jq -r '.server_type.memory' <<<"$server_json")
    target_type=$(yq -r "$path" "$TARGET_CONFIG")
    [[ "$current_type" != "$target_type" ]] || continue
    target_type_json=$(hcloud server-type describe "$target_type" -o json)
    target_disk=$(jq -r '.disk' <<<"$target_type_json")
    target_cores=$(jq -r '.cores' <<<"$target_type_json")
    target_memory=$(jq -r '.memory' <<<"$target_type_json")
    (( current_disk > target_disk )) || continue
    if [[ "$current_cores" != "$target_cores" || "$current_memory" != "$target_memory" ]]; then
      fail "$node cannot change from $current_type (${current_disk}GB root) to $target_type (${target_disk}GB root) without safe node replacement"
    fi
    for config in "$TARGET_CONFIG" "$STEADY_CONFIG" "$ROLLBACK_CONFIG"; do
      set_yaml_string "$config" "$path" "$current_type"
    done
    printf '%s\t%s\t%s\t%sGB\t%sGB\n' "$role" "$current_type" "$target_type" "$current_disk" "$target_disk" >> "$NODE_TYPE_RETENTION_FILE"
    warn "retaining $current_type for $role: it has the same compute as $target_type and its ${current_disk}GB root disk cannot shrink to ${target_disk}GB"
  done
}

check_volume_capacity() {
  local volumes_json pvs_json baseline_json current_used required consumed remaining projected result
  local tmp="${STATE_FILE}.tmp.$$"
  [[ -f "$STATE_FILE" && -s "$CAPACITY_PLAN_FILE" ]] \
    || fail "volume-capacity state or generated plan is missing"
  cmp -s <(jq -S . "$CAPACITY_PLAN_FILE") <(jq -S '.volume_capacity.plan' "$STATE_FILE") \
    || fail "generated volume-capacity plan drifted from the active migration state"
  [[ $(jq -r '.volume_capacity.quota_gib' "$STATE_FILE") == "$VOLUME_QUOTA_GIB" ]] \
    || fail "recorded Hetzner volume quota drifted from the active setting"
  [[ $(jq -r '.volume_capacity.safety_margin_gib' "$STATE_FILE") == "$VOLUME_SAFETY_MARGIN_GIB" ]] \
    || fail "recorded volume safety margin drifted from the active setting"

  volumes_json=$(hcloud volume list -o json) \
    || fail "could not read authoritative Hetzner volume state"
  jq -e 'type == "array" and all(.[]; (.id | numbers) and (.size | numbers) and .size > 0)' \
    <<<"$volumes_json" >/dev/null \
    || fail "Hetzner returned invalid or incomplete volume state"
  current_used=$(jq '[.[].size] | add // 0' <<<"$volumes_json")
  required=$(jq -r '.required_additional_gib' "$CAPACITY_PLAN_FILE")
  baseline_json=$(jq -c '.volume_capacity.baseline.volumes // []' "$STATE_FILE")

  if [[ "$baseline_json" == '[]' ]]; then
    baseline_json=$(jq -c 'map({id:(.id|tostring),size:(.size|tonumber),pv_name:(.labels["pv-name"] // "")})' \
      <<<"$volumes_json")
    consumed=0
  else
    pvs_json=$(kubectl get pv -o json | jq -c '[.items[].metadata.name]') \
      || fail "could not map current-cluster PVs for the volume-capacity drift check"
    consumed=$(jq -n --argjson current "$volumes_json" --argjson baseline "$baseline_json" \
      --argjson pvs "$pvs_json" '
        [ $current[] as $volume |
          ($volume.id | tostring) as $id |
          ($volume.labels["pv-name"] // "") as $pv |
          ([ $baseline[] | select(.id == $id) | .size ][0] // 0) as $old_size |
          select($pv != "" and ($pvs | index($pv)) != null) |
          (($volume.size | tonumber) - $old_size) |
          select(. > 0)
        ] | add // 0')
  fi
  (( consumed < required )) && remaining=$((required - consumed)) || remaining=0
  projected=$((current_used + remaining + VOLUME_SAFETY_MARGIN_GIB))
  if (( projected <= VOLUME_QUOTA_GIB )); then result=passed; else result=failed; fi

  jq --arg status "$result" --argjson quota "$VOLUME_QUOTA_GIB" \
    --argjson margin "$VOLUME_SAFETY_MARGIN_GIB" --argjson used "$current_used" \
    --argjson required "$required" --argjson consumed "$consumed" \
    --argjson remaining "$remaining" --argjson projected "$projected" \
    --argjson baseline "$baseline_json" '
      .volume_capacity.baseline //= {
        captured_at:(now | todateiso8601),account_used_gib:$used,volumes:$baseline
      } |
      .volume_capacity.status=$status |
      .volume_capacity.last_check={checked_at:(now | todateiso8601),quota_gib:$quota,
        safety_margin_gib:$margin,account_used_gib:$used,
        planned_additional_gib:$required,migration_capacity_consumed_gib:$consumed,
        remaining_planned_gib:$remaining,projected_peak_with_margin_gib:$projected,
        result:$status}
    ' "$STATE_FILE" > "$tmp"
  mv "$tmp" "$STATE_FILE"
  [[ "$result" == passed ]] \
    || fail "Hetzner volume capacity gate failed: used=${current_used}GiB remaining=${remaining}GiB margin=${VOLUME_SAFETY_MARGIN_GIB}GiB projected=${projected}GiB quota=${VOLUME_QUOTA_GIB}GiB"
  log "Hetzner volume capacity gate passed: used=${current_used}GiB remaining=${remaining}GiB margin=${VOLUME_SAFETY_MARGIN_GIB}GiB projected=${projected}GiB quota=${VOLUME_QUOTA_GIB}GiB"
}

stage_preflight() {
  local tool expected actual
  validate_operator_state_inputs
  for tool in kubectl helm hcloud ssh; do command -v "$tool" >/dev/null || fail "required live-migration tool is missing: $tool"; done
  [[ $(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE") == "$SOURCE_PROFILE" ]] || fail "active config no longer declares source profile $SOURCE_PROFILE"
  [[ -n "$DR_ENDPOINT" && "$DR_ENDPOINT" != *'.svc'* && "$DR_ENDPOINT" != *seaweedfs* ]] || fail "--dr-endpoint must be independent from the cluster"
  [[ -n "$DR_BUCKET" ]] || fail "--dr-bucket is required"
  [[ -n "${BACKUP_DR_ACCESS_KEY:-}" && -n "${BACKUP_DR_SECRET_KEY:-}" ]] || fail "BACKUP_DR_ACCESS_KEY and BACKUP_DR_SECRET_KEY are required"
  [[ -n "$BACKUP_RECIPIENT" || -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] || fail "set --backup-recipient or CLUSTER_BACKUP_PASSPHRASE"
  [[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required"
  check_volume_capacity
  preserve_non_shrinking_node_types
  validate_generated_configs
  kubectl cluster-info >/dev/null
  expected=$(( $(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG") + $(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG") ))
  actual=$(kubectl get nodes -o json | jq '.items | length')
  [[ "$actual" == "$expected" ]] || fail "source profile declares $expected nodes but cluster has $actual"
  wait_for_platform_convergence "$SOURCE_CONFIG" "source platform preflight"
  check_etcd_health
  hcloud server describe "${PROJECT}-master-1" >/dev/null
}

stage_backup() {
  local snapshot snapshot_root
  # Scheduled backups may be disabled in the steady source profile, which
  # means GitLab's chart-managed Toolbox CronJob is absent. Reconcile GitLab
  # with the temporary backup config before the backup role requires it.
  run_playbook "$BACKUP_CONFIG" --tags databases,gitlab,backup
  mkdir -p "$STATE_DIR/backups"
  cluster_backup "$BACKUP_CONFIG"
  snapshot_root="$STATE_DIR/rollback-snapshots"
  snapshot=$("$SCRIPT_DIR/snapshot-helm-baseline.sh" \
    --config "$SOURCE_CONFIG" --snapshot-dir "$snapshot_root" | tail -1)
  jq --arg snapshot "$snapshot" '.helm_snapshot=$snapshot' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"
  mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
}

requires_spread() { case $(profile_tier "$1") in medium|production) return 0 ;; *) return 1 ;; esac; }

ensure_spread_placement_group() {
  local placement_group="${PROJECT}-spread" result owner
  if result=$(hcloud placement-group describe "$placement_group" -o json 2>&1); then
    owner=$(jq -r '.labels.project // ""' <<<"$result")
    if [[ "$owner" != "$PROJECT" ]]; then
      hcloud placement-group add-label --overwrite "$placement_group" "project=$PROJECT" \
        || fail "could not reconcile project ownership on placement group $placement_group"
    fi
    return 0
  fi
  grep -qi 'not found' <<<"$result" \
    || fail "could not inspect placement group $placement_group: $result"
  hcloud placement-group create --name "$placement_group" --type spread \
    --label "project=$PROJECT" \
    || fail "could not create placement group $placement_group"
}

stage_expand() {
  local expected actual kubespray_checkpoint="${STATE_DIR}/expand-kubespray.done"
  if requires_spread "$SOURCE_CONFIG" || requires_spread "$TARGET_CONFIG"; then
    ensure_spread_placement_group
  fi
  # Newly created private-only nodes require the bastion NAT, network route,
  # node default route, and DNS configuration before Kubespray can install
  # packages. This remains idempotent for the retained nodes.
  if [[ -f "$kubespray_checkpoint" ]]; then
    # A failure in kubeconfig/tunnel or post-cluster reconciliation must not
    # replay a successful full Kubespray run on every resume.
    run_playbook "$EXPANSION_CONFIG" --tags infrastructure,network,security,cluster \
      -e skip_kubespray=true
  else
    run_playbook "$EXPANSION_CONFIG" --tags infrastructure,network,security,cluster \
      -e "profile_migration_kubespray_checkpoint=$kubespray_checkpoint"
  fi
  expected=$(( $(yq -r '.infrastructure.control_plane.count' "$EXPANSION_CONFIG") + $(yq -r '.infrastructure.workers.count' "$EXPANSION_CONFIG") ))
  kubectl wait nodes --all --for=condition=Ready --timeout=900s
  actual=$(kubectl get nodes -o json | jq '.items | length')
  [[ "$actual" == "$expected" ]] || fail "expected $expected Kubernetes nodes after expansion, found $actual"
  ensure_control_plane_etcd_ha "$EXPANSION_CONFIG"
  check_etcd_health
}

wait_for_node_runtime() {
  local node="$1" attempt
  kubectl wait "node/${node}" --for=condition=DiskPressure=False --timeout="$RESIZE_TIMEOUT"
  kubectl wait pod -n kube-system -l k8s-app=cilium \
    --field-selector "spec.nodeName=${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  kubectl wait pod -n kube-system -l app=hcloud-csi \
    --field-selector "spec.nodeName=${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  for attempt in {1..60}; do
    if kubectl get csinode "$node" -o json 2>/dev/null \
      | jq -e 'any(.spec.drivers[]?; .name == "csi.hetzner.cloud")' >/dev/null; then
      return 0
    fi
    sleep 5
  done
  fail "Hetzner CSI did not register on $node after it became Ready"
}

maintain_node_root_disk() {
  local node="$1" host usage
  ssh_args_for_facts
  host=$(NODE="$node" yq -r \
    '.master_ips[strenv(NODE)] // .worker_ips[strenv(NODE)] // ""' \
    "${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml")
  [[ -n "$host" ]] || fail "private IP is missing from infrastructure facts for $node"
  usage=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
    "df -P / | awk 'NR == 2 {gsub(/%/, \"\", \$5); print \$5}'")
  [[ "$usage" =~ ^[0-9]+$ ]] || fail "could not determine root-disk usage for $node"
  if (( usage >= ROOT_DISK_PRUNE_PERCENT )); then
    warn "$node root disk is ${usage}% used; pruning unused container images and bounded journals"
    ssh "${SSH_ARGS[@]}" "root@${host}" \
      'set -eu; command -v crictl >/dev/null && crictl rmi --prune >/dev/null || true; journalctl --vacuum-size=500M >/dev/null; sync'
    usage=$(ssh "${SSH_ARGS[@]}" "root@${host}" \
      "df -P / | awk 'NR == 2 {gsub(/%/, \"\", \$5); print \$5}'")
  fi
  [[ "$usage" =~ ^[0-9]+$ ]] || fail "could not verify root-disk usage for $node"
  (( usage <= ROOT_DISK_MAX_PERCENT )) \
    || fail "$node root disk remains ${usage}% used after safe cleanup (maximum ${ROOT_DISK_MAX_PERCENT}%)"
}

expand_node_root_disk() {
  local node="$1" host attempt ssh_ready=false
  ssh_args_for_facts
  host=$(NODE="$node" yq -r \
    '.master_ips[strenv(NODE)] // .worker_ips[strenv(NODE)] // ""' \
    "${PROJECT_ROOT}/playbooks/${PROJECT}-infra-facts.yml")
  [[ -n "$host" ]] || fail "private IP is missing from infrastructure facts for $node"
  for attempt in {1..60}; do
    if ssh "${SSH_ARGS[@]}" "root@${host}" true >/dev/null 2>&1; then
      ssh_ready=true
      break
    fi
    sleep 5
  done
  [[ "$ssh_ready" == true ]] || fail "SSH did not recover on $node after its resize"
  ssh "${SSH_ARGS[@]}" "root@${host}" 'set -eu
    root_source=$(findmnt -n -o SOURCE /)
    root_fstype=$(findmnt -n -o FSTYPE /)
    parent=$(lsblk -n -o PKNAME "$root_source" | head -n1)
    partnum=$(lsblk -n -o PARTN "$root_source" | head -n1)
    [ -n "$parent" ] && [ -n "$partnum" ] || {
      echo "cannot resolve the root partition parent for $root_source" >&2
      exit 1
    }
    command -v growpart >/dev/null 2>&1 || {
      echo "growpart is required to adopt an expanded Hetzner root disk" >&2
      exit 1
    }
    growpart "/dev/$parent" "$partnum" >/dev/null 2>&1 || true
    case "$root_fstype" in
      ext2|ext3|ext4) resize2fs "$root_source" >/dev/null ;;
      xfs) xfs_growfs / >/dev/null ;;
      *) echo "unsupported root filesystem for online growth: $root_fstype" >&2; exit 1 ;;
    esac'
}

wait_for_api_ready() {
  local deadline=$((SECONDS + API_READY_TIMEOUT))
  while (( SECONDS < deadline )); do
    if kubectl --request-timeout=8s get --raw=/readyz >/dev/null 2>&1; then
      return 0
    fi
    sleep 5
  done
  fail "Kubernetes API did not become ready within ${API_READY_TIMEOUT}s"
}

# A Shamir-sealed Vault member is deliberately unready. If it was rescheduled
# by a previous drain, its PDB will correctly block the next drain until an
# operator supplies the protected keys. Reconcile every running member before
# each node operation so one-at-a-time migrations remain unattended and safe.
unseal_vault_members() {
  local init_file password_file pod status key deadline
  local -a unseal_keys=()

  kubectl get statefulset vault -n vault >/dev/null 2>&1 || return 0
  # Use the exact operator-state path resolved and persisted by this migration.
  # Falling back to the checkout-local default here would pass preflight and the
  # backup gate, then fail only after a node drain when isolated controllers use
  # --operator-state-root or --vault-init-file.
  init_file="$VAULT_INIT_FILE"
  password_file="${ANSIBLE_VAULT_PASSWORD_FILE:-${VAULT_PASSWORD_FILE:-}}"
  [[ -f "$init_file" ]] || fail "Vault is deployed but protected init material is missing: $init_file"
  [[ -n "$password_file" && -f "$password_file" ]] \
    || fail "Vault is deployed but ANSIBLE_VAULT_PASSWORD_FILE is missing or invalid"

  while IFS= read -r key; do
    unseal_keys+=("$key")
  done < <(
    ansible-vault view --vault-password-file "$password_file" "$init_file" \
      | jq -er '.unseal_keys_b64[]'
  )
  ((${#unseal_keys[@]} > 0)) || fail "protected Vault init material contains no unseal keys"

  while IFS= read -r pod; do
    [[ -n "$pod" ]] || continue
    # Draining a node can reschedule a Vault member. A replacement pod may be
    # listed several minutes before its server container accepts exec calls.
    # Wait for an authoritative initialized status before applying protected
    # unseal keys; do not misclassify normal volume attachment/startup as data
    # loss, and still fail closed when the member never becomes reachable.
    deadline=$((SECONDS + VAULT_MEMBER_TIMEOUT))
    status=""
    while (( SECONDS < deadline )); do
      status=$(kubectl exec -n vault "$pod" -- vault status -format=json 2>/dev/null || true)
      jq -e '.initialized == true' <<<"$status" >/dev/null 2>&1 && break
      sleep 5
    done
    jq -e '.initialized == true' <<<"$status" >/dev/null 2>&1 \
      || fail "Vault member $pod did not report initialized within ${VAULT_MEMBER_TIMEOUT}s"
    if jq -e '.sealed == true' <<<"$status" >/dev/null; then
      for key in "${unseal_keys[@]}"; do
        # shellcheck disable=SC2016 # $key expands inside the remote pod shell.
        printf '%s\n' "$key" \
          | kubectl exec -i -n vault "$pod" -- sh -c \
            'IFS= read -r key; vault operator unseal "$key"' >/dev/null 2>&1 || true
        status=$(kubectl exec -n vault "$pod" -- vault status -format=json 2>/dev/null || true)
        jq -e '.sealed == false' <<<"$status" >/dev/null 2>&1 && break
      done
      jq -e '.sealed == false' <<<"$status" >/dev/null 2>&1 \
        || fail "Vault member $pod remained sealed after applying protected operator keys"
    fi
    kubectl wait -n vault "pod/${pod}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  done < <(kubectl get pods -n vault -l app.kubernetes.io/name=vault -o name | sed 's#^pod/##')
}

resize_node() {
  local node="$1" target_type="$2" role="$3" server_json current_type current_disk target_disk placement target_placement=""
  local requested_type="$target_type" configured_override fallback_type="" keep_disk=false
  local node_state_dir="$STATE_DIR/resize-nodes" in_progress done_marker node_unschedulable interrupted=false
  local current_status provider_volumes
  mkdir -p "$node_state_dir"
  in_progress="${node_state_dir}/${node}.in-progress"
  done_marker="${node_state_dir}/${node}.done"
  server_json=$(hcloud server describe "$node" -o json)
  current_type=$(jq -r '.server_type.name' <<<"$server_json")
  current_disk=$(jq -r '.primary_disk_size' <<<"$server_json")
  configured_override=$(NODE="$node" yq -r '.infrastructure.node_type_overrides[strenv(NODE)] // ""' "$TARGET_CONFIG")
  if [[ -n "$configured_override" ]]; then
    target_type="$configured_override"
    keep_disk=true
  fi
  target_disk=$(hcloud server-type describe "$target_type" -o json | jq -r '.disk')
  if [[ "$current_type" != "$target_type" ]] && ! server_type_available_for_node "$node" "$target_type"; then
    [[ -z "$configured_override" ]] \
      || fail "recorded fallback type $target_type is no longer available for $node"
    fallback_type=$(select_equivalent_fallback_type "$node" "$requested_type" || true)
    [[ -n "$fallback_type" ]] \
      || fail "target type $requested_type is unavailable for $node and no equivalent fallback was authorized"
    target_type="$fallback_type"
    keep_disk=true
    record_node_type_override "$node" "$requested_type" "$target_type"
    warn "using temporary equivalent type $target_type for $node because $requested_type is unavailable; retaining the existing ${current_disk}GB disk"
  fi
  [[ "$keep_disk" != true ]] || target_disk="$current_disk"
  placement=$(jq -r '.placement_group.name // ""' <<<"$server_json")
  node_unschedulable=$(kubectl get node "$node" -o jsonpath='{.spec.unschedulable}' 2>/dev/null || true)
  requires_spread "$TARGET_CONFIG" && target_placement="${PROJECT}-spread"
  if [[ -f "$in_progress" || "$node_unschedulable" == true ]]; then
    # The cordon is also an upgrade-safe recovery signal for migrations that
    # were interrupted before per-node markers existed. Finish this node and
    # require the full post-gate before considering any subsequent drain.
    interrupted=true
    log "resuming interrupted one-node resize for $node"
  else
    # A stage-level checkpoint is intentionally not enough here: on resume,
    # every next drain must be rejected until workloads and stateful data paths
    # have recovered from the previous node operation.
    wait_for_platform_convergence "$SOURCE_CONFIG" "pre-resize health for $node"
  fi
  if [[ "$current_type" == "$target_type" && "$placement" == "$target_placement" ]] \
    && (( current_disk >= target_disk )); then
    ensure_server_running "$node"
    kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
    expand_node_root_disk "$node"
    maintain_node_root_disk "$node"
    wait_for_node_runtime "$node"
    kubectl uncordon "$node" >/dev/null
    unseal_vault_members
    [[ "$role" != master ]] || check_etcd_health
    wait_for_platform_convergence "$SOURCE_CONFIG" "post-resize health for $node"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$done_marker"
    rm -f "$in_progress"
    log "$node already converged at type=$target_type"
    return 0
  fi
  if [[ "$interrupted" == false ]]; then
    wait_for_api_ready
    unseal_vault_members
    maintain_node_root_disk "$node"
    [[ "$role" != master ]] || check_etcd_health "$node"
    [[ "$role" != master ]] || check_control_plane_alternates_ready "$node" "$EXPANSION_CONFIG"
    date -u +%Y-%m-%dT%H:%M:%SZ > "$in_progress"
    kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=15m
  else
    # The durable marker is written only after all pre-drain checks and before
    # kubectl drain. The node may already be off, so SSH/API work against that
    # node must wait until provider reconciliation has started it again.
    log "skipping completed pre-drain work for interrupted node $node"
  fi
  current_status=$(server_status "$node")
  provider_volumes=$(hcloud server describe "$node" -o json | jq '.volumes | length')
  if [[ "$current_status" == off && "$provider_volumes" != 0 ]]; then
    warn "$node resumed while off with provider volumes attached; starting it so CSI can reconcile detachment"
    ensure_server_running "$node"
    wait_for_api_ready
    kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  fi
  # A successful Kubernetes eviction does not mean the storage provider has
  # detached every RWO volume yet. Powering the VM off during that window can
  # corrupt an application filesystem or prolong a multi-attach outage.
  wait_for_csi_detach "$node"
  # Power operations are asynchronous. Placement-group reconciliation can also
  # leave a server running, so provider status—not CLI completion—is the gate.
  ensure_server_stopped "$node"
  if [[ "$role" == master ]] \
    && (( $(yq -r '.infrastructure.control_plane.count' "$EXPANSION_CONFIG") >= 3 )); then
    # Prove that the API tunnel, both surviving API servers, and the two-member
    # etcd quorum work while this master is actually absent, before changing
    # any provider attributes on the stopped server.
    if ! (check_control_plane_survivors "$node" "$EXPANSION_CONFIG"); then
      warn "control-plane survivor gate failed with $node off; restoring the stopped master before aborting"
      ensure_server_running "$node"
      wait_for_api_ready
      kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT" || true
      fail "refusing provider mutation because control-plane failover was not healthy without $node"
    fi
  fi
  if [[ -n "$target_placement" && "$placement" != "$target_placement" ]]; then
    [[ -z "$placement" ]] || hcloud server remove-from-placement-group "$node"
    hcloud server add-to-placement-group --placement-group "$target_placement" "$node"
  elif [[ -z "$target_placement" && -n "$placement" ]]; then
    hcloud server remove-from-placement-group "$node"
  fi
  if [[ "$current_type" != "$target_type" ]] || (( current_disk < target_disk )); then
    # Recheck after placement mutations: Hetzner may return the server to a
    # running state, and change-type rejects anything except provider state off.
    ensure_server_stopped "$node"
    change_server_type_with_retry "$node" "$target_type" "$target_disk" "$keep_disk"
  fi
  ensure_server_running "$node"
  wait_for_api_ready
  kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  expand_node_root_disk "$node"
  maintain_node_root_disk "$node"
  wait_for_node_runtime "$node"
  kubectl uncordon "$node"
  unseal_vault_members
  [[ "$role" != master ]] || check_etcd_health
  # A drained node can become Ready before stateful volumes have reattached and
  # application PDB capacity is restored. Never proceed to the next node until
  # the complete active service set is healthy again.
  # Target services are not installed until apply-target. During resize the
  # source service set is still authoritative, even though nodes are converging
  # on the target compute type and placement.
  wait_for_platform_convergence "$SOURCE_CONFIG" "post-resize health for $node"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$done_marker"
  rm -f "$in_progress"
}

stage_resize() {
  local workers masters cp_type worker_type i marker resume_node="" found=false
  local -a nodes=() node_types=() node_roles=() in_progress_markers=()
  workers=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  masters=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  ensure_control_plane_etcd_ha "$EXPANSION_CONFIG"
  worker_type=$(yq -r '.infrastructure.workers.type' "$TARGET_CONFIG")
  cp_type=$(yq -r '.infrastructure.control_plane.type' "$TARGET_CONFIG")
  for ((i=1; i<=workers; i++)); do
    nodes+=("${PROJECT}-worker-${i}"); node_types+=("$worker_type"); node_roles+=(worker)
  done
  for ((i=2; i<=masters; i++)); do
    nodes+=("${PROJECT}-master-${i}"); node_types+=("$cp_type"); node_roles+=(master)
  done
  nodes+=("${PROJECT}-master-1"); node_types+=("$cp_type"); node_roles+=(master)

  mkdir -p "$STATE_DIR/resize-nodes"
  while IFS= read -r marker; do
    [[ -n "$marker" ]] && in_progress_markers+=("$marker")
  done < <(find "$STATE_DIR/resize-nodes" -maxdepth 1 -type f -name '*.in-progress' -print | sort)
  ((${#in_progress_markers[@]} <= 1)) \
    || fail "multiple resize nodes are marked in progress; refusing concurrent recovery"
  if ((${#in_progress_markers[@]} == 1)); then
    marker="${in_progress_markers[0]}"
    resume_node=$(basename "$marker" .in-progress)
    # Recover the exact interrupted node before any completed/earlier node can
    # run a global health gate that necessarily sees this node offline.
    for i in "${!nodes[@]}"; do
      if [[ "${nodes[$i]}" == "$resume_node" ]]; then
        resize_node "${nodes[$i]}" "${node_types[$i]}" "${node_roles[$i]}"
        found=true
        break
      fi
    done
    [[ "$found" == true ]] \
      || fail "resize marker references unknown node: $resume_node"
  fi

  for i in "${!nodes[@]}"; do
    [[ "${nodes[$i]}" == "$resume_node" ]] && continue
    resize_node "${nodes[$i]}" "${node_types[$i]}" "${node_roles[$i]}"
  done
}

control_plane_nodes() {
  kubectl get nodes -l node-role.kubernetes.io/control-plane -o json \
    | jq -r '.items | sort_by(.metadata.name)[] | .metadata.name'
}

check_control_plane_schedulability_contract() {
  local config="$1" desired expected actual violations
  desired=$(yq -r '.infrastructure.control_plane.schedulable // false' "$config")
  expected=$(yq -r '.infrastructure.control_plane.count' "$config")
  actual=$(control_plane_nodes | wc -l | tr -d ' ')
  [[ "$actual" == "$expected" ]] \
    || fail "control-plane topology mismatch: expected=$expected actual=$actual"
  violations=$(kubectl get nodes -l node-role.kubernetes.io/control-plane -o json \
    | jq --arg desired "$desired" '[.items[] | select(
        ([.spec.taints[]? | select(
          (.key == "node-role.kubernetes.io/control-plane" or .key == "node-role.kubernetes.io/master")
          and .effect == "NoSchedule"
        )] | length > 0) != ($desired != "true")
      )] | length')
  [[ "$violations" == 0 ]] \
    || fail "$violations control-plane node(s) violate schedulable=$desired"
}

reconcile_control_plane_schedulability() {
  local config="$1" desired node checkpoint
  desired=$(yq -r '.infrastructure.control_plane.schedulable // false' "$config")
  if [[ "$desired" == true ]]; then
    while IFS= read -r node; do
      [[ -n "$node" ]] || continue
      kubectl uncordon "$node" >/dev/null 2>&1 || true
      kubectl taint node "$node" node-role.kubernetes.io/control-plane:NoSchedule- >/dev/null 2>&1 || true
      kubectl taint node "$node" node-role.kubernetes.io/master:NoSchedule- >/dev/null 2>&1 || true
    done < <(control_plane_nodes)
  else
    # Taint the complete control plane before the first eviction so ordinary
    # workloads cannot hop from one master to the next during the transition.
    while IFS= read -r node; do
      [[ -n "$node" ]] || continue
      kubectl taint node "$node" node-role.kubernetes.io/control-plane=:NoSchedule --overwrite >/dev/null
    done < <(control_plane_nodes)
    while IFS= read -r node; do
      [[ -n "$node" ]] || continue
      checkpoint="$STATE_DIR/schedulability-${node}.done"
      [[ -f "$checkpoint" ]] && continue
      if ! kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=15m; then
        kubectl uncordon "$node" >/dev/null 2>&1 || true
        fail "failed to evacuate ordinary workloads from dedicated control-plane node $node"
      fi
      kubectl uncordon "$node" >/dev/null
      kubectl wait "node/$node" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
      unseal_vault_members
      check_etcd_health "$node"
      touch "$checkpoint"
    done < <(control_plane_nodes)
  fi
  check_control_plane_schedulability_contract "$config"
}

stage_migrate_vault_storage() {
  "$SCRIPT_DIR/vault-storage-migrate.sh" --config "$TARGET_CONFIG" --force
}

stage_apply_target() {
  [[ -f "$STATE_DIR/pre-target-platform.yaml" ]] || cp "$CONFIG_FILE" "$STATE_DIR/pre-target-platform.yaml"
  switched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  jq --arg switched "$switched_at" '.target_write_switch_started_at=$switched' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"
  mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
  cp "$STEADY_CONFIG" "$CONFIG_FILE"
  # Expansion already reconciles the complete node/network/cluster foundation.
  # Keep target retries focused on platform components so an application-layer
  # failure does not repeat Kubespray or restart the bastion on every resume.
  if [[ ! -f "$STATE_DIR/apply-target-platform.done" ]]; then
    run_playbook "$CONFIG_FILE" --skip-tags infrastructure,network,security,cluster
    touch "$STATE_DIR/apply-target-platform.done"
  else
    log "checkpoint already complete: apply-target-platform"
  fi
  # Apply the target's resource envelope before evacuating a newly dedicated
  # control plane. This prevents a compact 3+3 promotion from deadlocking on
  # source-sized pods that cannot all fit on the three workers during rollout.
  reconcile_control_plane_schedulability "$TARGET_CONFIG"
}

run_vmctl_migration() {
  local job="$1" source_addr="$2" target_addr="$3" time_start="${4:-1970-01-01T00:00:00Z}" phase
  job=$(printf '%s' "$job" | tr '[:upper:]_' '[:lower:]-' | cut -c1-63)
  if kubectl get job "$job" -n monitoring >/dev/null 2>&1; then
    phase=$(kubectl get job "$job" -n monitoring -o json | jq -r '
      if ([.status.conditions[]? | select(.type == "Complete" and .status == "True")] | length) > 0 then "complete"
      elif ([.status.conditions[]? | select(.type == "Failed" and .status == "True")] | length) > 0 then "failed"
      else "running" end')
    [[ "$phase" != failed ]] || fail "VictoriaMetrics job $job failed; inspect it and restore the destination before retrying to avoid duplicate samples"
    if [[ "$phase" == complete ]]; then log "VictoriaMetrics job already complete: $job"; return 0; fi
  else
    time_arg="            - --vm-native-filter-time-start=${time_start}"
    kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: monitoring
  labels: {app.kubernetes.io/part-of: profile-migration}
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 604800
  template:
    spec:
      automountServiceAccountToken: false
      restartPolicy: Never
      securityContext:
        runAsNonRoot: true
        runAsUser: 1000
        runAsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: vmctl
          image: ${VMCTL_IMAGE}
          securityContext:
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
            readOnlyRootFilesystem: true
          args:
            - vm-native
            - -s
            - --disable-progress-bar
            - --vm-native-src-addr=${source_addr}
            - --vm-native-dst-addr=${target_addr}
${time_arg}
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 1Gi}
EOF
  fi
  if ! kubectl wait "job/${job}" -n monitoring --for=condition=complete --timeout=8h; then
    kubectl logs "job/${job}" -n monitoring --all-containers --tail=300 || true
    fail "VictoriaMetrics history migration failed"
  fi
}

vm_addresses() {
  local from_mode="$1" to_mode="$2"
  if [[ "$from_mode" == single ]]; then VM_SOURCE_ADDR='http://vmsingle-vmsingle.monitoring.svc:8429'; else VM_SOURCE_ADDR='http://vmselect-vmcluster.monitoring.svc:8481/select/0/prometheus'; fi
  if [[ "$to_mode" == single ]]; then VM_TARGET_ADDR='http://vmsingle-vmsingle.monitoring.svc:8429'; else VM_TARGET_ADDR='http://vminsert-vmcluster.monitoring.svc:8480/insert/0/prometheus'; fi
}

stage_migrate_data() {
  local source_mode target_mode
  source_mode=$(profile_mode "$SOURCE_CONFIG"); target_mode=$(profile_mode "$TARGET_CONFIG")
  if [[ "$source_mode" != "$target_mode" ]]; then
    vm_addresses "$source_mode" "$target_mode"
    run_vmctl_migration "${PROJECT}-vm-${SOURCE_PROFILE}-to-${TARGET_PROFILE}" "$VM_SOURCE_ADDR" "$VM_TARGET_ADDR"
  fi
  [[ "$source_mode" != single || "$target_mode" != cluster ]] || warn "Loki objects remain an archive until separately confirmed finalization"
}

stage_validate() {
  kubectl wait nodes --all --for=condition=Ready --timeout=900s
  check_etcd_health
  check_control_plane_schedulability_contract "$STEADY_CONFIG"
  wait_for_platform_convergence "$STEADY_CONFIG" "target platform validation"
  kubectl get backupstoragelocation/default -n velero -o json | jq -e '.status.phase == "Available"' >/dev/null
}

stage_post_backup() {
  # New target components (for example GitLab) do not exist when the source
  # backup role is first reconciled. Rebuild the backup control plane from the
  # target config before the mandatory post-migration bundle.
  cp "$STEADY_CONFIG" "$POST_BACKUP_CONFIG"
  yq -i '.platform_profile = "custom" | .backup.enabled = true | .backup.disaster_recovery.enabled = true' "$POST_BACKUP_CONFIG"
  set_yaml_string "$POST_BACKUP_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$POST_BACKUP_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$POST_BACKUP_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$POST_BACKUP_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"
  run_playbook "$POST_BACKUP_CONFIG" --tags databases,gitlab,backup
  cluster_backup "$POST_BACKUP_CONFIG"
}

rollback_components_to_remove() {
  local component
  # Remove dependants before their shared services. Backup/DR are included
  # when the migration installed their temporary control plane for its backup
  # gates, even if the named target does not normally select them.
  for component in daytona blackbox apm glitchtip temporal postal tracing coroot gitlab-runner gitlab mongodb eso elasticsearch dragonfly disaster-recovery backup autoscaling gitops observability postgresql databases secrets object-storage; do
    component_enabled "$SOURCE_CONFIG" "$component" && continue
    if component_enabled "$TARGET_CONFIG" "$component" \
      || { [[ "$component" == backup || "$component" == disaster-recovery ]] \
        && [[ -f "$STATE_DIR/stage-backup.done" ]]; }; then
      printf '%s\n' "$component"
    fi
  done
}

remove_target_only_components_for_rollback() {
  local component delete_data=false
  # Target-only applications can have accepted writes after apply-target. The
  # post-migration encrypted bundle is the authority required to delete their
  # data. Before that checkpoint, remove_component.yml deliberately fails on a
  # data-bearing component instead of silently deleting it or declaring a
  # partial rollback successful.
  [[ -f "$STATE_DIR/stage-post-backup.done" ]] && delete_data=true
  while IFS= read -r component; do
    [[ -n "$component" ]] || continue
    ansible-playbook "$PROJECT_ROOT/playbooks/remove_component.yml" \
      -e "@$ROLLBACK_CONFIG" -e "project_name=$PROJECT" \
      -e "secrets_file=$SECRETS_FILE" -e "vault_init_output_file=$VAULT_INIT_FILE" \
      -e "target_component=$component" -e "confirm_component_removal=$component" \
      -e "delete_component_data=$delete_data"
  done < <(rollback_components_to_remove)
  if ! component_enabled "$SOURCE_CONFIG" hipaa && component_enabled "$TARGET_CONFIG" hipaa; then
    warn "HIPAA-oriented hardening introduced by the target is retained; automated rollback cannot safely reverse host and cluster controls"
  fi
}

restore_helm_baseline_without_vault() {
  local snapshot="$1" namespace release revision found=false
  local baseline="${snapshot}/helm-revisions.tsv"
  [[ -s "$baseline" ]] || fail "recorded Helm revision baseline is missing: $baseline"
  while IFS=$'\t' read -r namespace release revision; do
    [[ -n "$namespace" && -n "$release" && "$revision" =~ ^[1-9][0-9]*$ ]] \
      || fail "recorded Helm revision baseline contains an invalid row"
    if [[ "$namespace" == vault ]]; then
      warn "retaining the migrated Vault Raft release instead of restoring its file-backed Helm revision"
      continue
    fi
    found=true
    helm rollback "$release" "$revision" -n "$namespace" --wait --timeout 30m0s
  done < "$baseline"
  [[ "$found" == true ]] || warn "the recorded Helm baseline contained no non-Vault releases"
}

# A controller can be provisioned with a CLI-only bastion override which is
# absent from its YAML. Capture the actual retained server before any migration
# playbook runs and replay this on resume to heal older active states locally.
if [[ "$DRY_RUN" != true && ( "$COMMAND" == execute || "$COMMAND" == resume \
  || "$COMMAND" == rollback || "$COMMAND" == finalize ) ]]; then
  capture_live_bastion_type
  validate_generated_configs
fi

if [[ "$COMMAND" == rollback ]]; then
  rollback_status=$(jq -r '.status' "$STATE_FILE")
  [[ "$rollback_status" != finalized && "$rollback_status" != finalizing ]] || fail "started or completed finalization requires recovery-bundle restoration, not an in-place rollback"
  compgen -G "$STATE_DIR/finalize-*.done" >/dev/null && fail "destructive finalization already started; restore the pre-finalize recovery bundle"
  [[ -f "$ROLLBACK_CONFIG" ]] || fail "rollback configuration is missing"
  if [[ $(jq -r '.status' "$STATE_FILE") == rolled_back ]]; then log "migration is already rolled back"; exit 0; fi
  if [[ "$DRY_RUN" == true ]]; then dry "would copy post-switch VictoriaMetrics samples back, restore the Helm baseline, and activate source capabilities while retaining nodes"; exit 0; fi
  snapshot=$(jq -r '.helm_snapshot // ""' "$STATE_FILE")
  [[ -n "$snapshot" && -d "$snapshot" ]] || fail "recorded Helm snapshot is unavailable"
  if [[ -f "$STATE_DIR/stage-apply-target.done" && $(profile_mode "$SOURCE_CONFIG") != $(profile_mode "$TARGET_CONFIG") ]]; then
    vm_addresses "$(profile_mode "$TARGET_CONFIG")" "$(profile_mode "$SOURCE_CONFIG")"
    run_vmctl_migration "${PROJECT}-vm-rollback-${TARGET_PROFILE}-to-${SOURCE_PROFILE}" "$VM_SOURCE_ADDR" "$VM_TARGET_ADDR" "$(jq -r '.target_write_switch_started_at' "$STATE_FILE")"
  fi
  if [[ -f "$STATE_DIR/stage-migrate-vault-storage.done" ]]; then
    warn "Vault storage is already Raft; restoring every non-Vault Helm baseline while retaining the migrated Vault release"
    restore_helm_baseline_without_vault "$snapshot"
  else
    "$SCRIPT_DIR/rollback.sh" --snapshot "$snapshot" --force
  fi
  persist_active_config "$ROLLBACK_CONFIG"
  remove_target_only_components_for_rollback
  reconcile_control_plane_schedulability "$ROLLBACK_CONFIG"
  run_playbook "$ROLLBACK_CONFIG" --skip-tags infrastructure,network,security,cluster
  check_control_plane_schedulability_contract "$ROLLBACK_CONFIG"
  check_platform_health "$ROLLBACK_CONFIG"
  jq '.status="rolled_back" | .rolled_back_at=(now | todateiso8601)' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"; mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
  log "source capabilities restored; expanded or resized nodes were deliberately retained"
  exit 0
fi

remove_disabled_components() {
  local include_backup="$1" component
  refuse_automatic_hipaa_retirement
  if [[ "$include_backup" == true ]]; then
    # The migration deploys a temporary backup control plane even when both the
    # source and target profiles have scheduled backups disabled. Retire that
    # temporary surface based on the target contract, not only on a source to
    # target capability delta.
    if ! component_enabled "$TARGET_CONFIG" disaster-recovery; then
      ansible-playbook "$PROJECT_ROOT/playbooks/remove_component.yml" -e "@$TARGET_CONFIG" -e "project_name=$PROJECT" \
        -e "secrets_file=$SECRETS_FILE" -e "vault_init_output_file=$VAULT_INIT_FILE" \
        -e target_component=disaster-recovery -e confirm_component_removal=disaster-recovery -e delete_component_data=true
    fi
    if ! component_enabled "$TARGET_CONFIG" backup; then
      ansible-playbook "$PROJECT_ROOT/playbooks/remove_component.yml" -e "@$TARGET_CONFIG" -e "project_name=$PROJECT" \
        -e "secrets_file=$SECRETS_FILE" -e "vault_init_output_file=$VAULT_INIT_FILE" \
        -e target_component=backup -e confirm_component_removal=backup -e delete_component_data=true
    fi
    return
  fi
  while IFS= read -r component; do
    [[ "$component" == backup || "$component" == disaster-recovery ]] && continue
    ansible-playbook "$PROJECT_ROOT/playbooks/remove_component.yml" -e "@$TARGET_CONFIG" -e "project_name=$PROJECT" \
      -e "secrets_file=$SECRETS_FILE" -e "vault_init_output_file=$VAULT_INIT_FILE" \
      -e "target_component=$component" -e "confirm_component_removal=$component" -e delete_component_data=true
  done < <(components_to_remove)
}

capture_observability_pvcs() {
  local pattern="$1"
  kubectl get pods -n monitoring -o json | jq -r --arg pattern "$pattern" '
    .items[] | select(.metadata.name | test($pattern)) | .spec.volumes[]? | .persistentVolumeClaim.claimName // empty' | sort -u
}

retire_observability_source() {
  local source_mode target_mode pvc pvc_file="${STATE_DIR}/retire-observability-pvcs.txt"
  local -a pvcs=()
  source_mode=$(profile_mode "$SOURCE_CONFIG"); target_mode=$(profile_mode "$TARGET_CONFIG")
  [[ "$source_mode" != "$target_mode" ]] || return 0
  if [[ ! -f "$pvc_file" ]]; then
    if [[ "$source_mode" == single ]]; then
      capture_observability_pvcs '^(vmsingle-vmsingle-|loki-)' > "$pvc_file"
    else
      capture_observability_pvcs '^vmstorage-vmcluster-' > "$pvc_file"
    fi
  fi
  while IFS= read -r pvc; do [[ -z "$pvc" ]] || pvcs+=("$pvc"); done < "$pvc_file"
  if [[ "$source_mode" == single ]]; then
    kubectl delete vmsingle vmsingle -n monitoring --ignore-not-found --wait --timeout=10m
    helm status promtail -n logging-agents >/dev/null 2>&1 \
      && helm uninstall promtail -n logging-agents --wait --timeout 10m0s
    helm status promtail -n monitoring >/dev/null 2>&1 \
      && helm uninstall promtail -n monitoring --wait --timeout 10m0s
    helm status loki -n monitoring >/dev/null 2>&1 \
      && helm uninstall loki -n monitoring --wait --timeout 10m0s
    warn "external Loki object-store buckets are retained as a recovery archive"
  else
    kubectl delete vmcluster vmcluster -n monitoring --ignore-not-found --wait --timeout=10m
  fi
  (( ${#pvcs[@]} == 0 )) || kubectl delete pvc -n monitoring "${pvcs[@]}" --ignore-not-found --wait --timeout=10m
}

kubespray_inventory() { printf '%s/playbooks/kubespray/inventory/%s/hosts.yml' "$PROJECT_ROOT" "$PROJECT"; }

remove_inventory_node() {
  local inventory="$1" node="$2"
  NODE="$node" yq -i 'del(.all.hosts[strenv(NODE)]) |
    del(.all.children.kube_control_plane.hosts[strenv(NODE)]) |
    del(.all.children.kube_node.hosts[strenv(NODE)]) |
    del(.all.children.etcd.hosts[strenv(NODE)])' "$inventory"
}

remove_cluster_node() {
  local node="$1" role="$2" inventory kubespray_dir
  inventory=$(kubespray_inventory); kubespray_dir="${PROJECT_ROOT}/playbooks/kubespray"
  [[ -f "$inventory" && -x "$kubespray_dir/.venv/bin/ansible-playbook" ]] || fail "Kubespray runtime or inventory is unavailable"
  if ! hcloud server describe "$node" >/dev/null 2>&1; then
    remove_inventory_node "$inventory" "$node"
    kubectl delete node "$node" --ignore-not-found
    warn "$node server was already absent; inventory was reconciled"
    return 0
  fi
  [[ "$role" != master ]] || check_etcd_health "$node"
  kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=15m
  unset ANSIBLE_CONFIG ANSIBLE_SSH_ARGS ANSIBLE_SSH_COMMON_ARGS
  ANSIBLE_CONFIG="$kubespray_dir/ansible.cfg" "$kubespray_dir/.venv/bin/ansible-playbook" \
    -i "$inventory" --become --become-user=root "$kubespray_dir/remove-node.yml" \
    -e "node=$node" -e "skip_confirmation=true"
  hcloud server delete "$node"
  kubectl delete node "$node" --ignore-not-found
  remove_inventory_node "$inventory" "$node"
  [[ "$role" != master ]] || check_etcd_health
}

scale_in_nodes() {
  local current_workers target_workers current_cp target_cp i
  current_workers=$(yq -r '.infrastructure.workers.count' "$EXPANSION_CONFIG")
  target_workers=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  current_cp=$(yq -r '.infrastructure.control_plane.count' "$EXPANSION_CONFIG")
  target_cp=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  for ((i=current_workers; i>target_workers; i--)); do remove_cluster_node "${PROJECT}-worker-${i}" worker; done
  for ((i=current_cp; i>target_cp; i--)); do remove_cluster_node "${PROJECT}-master-${i}" master; done
}

finalize_stage() {
  case "$1" in
    retire-services) remove_disabled_components false ;;
    retire-observability) retire_observability_source ;;
    scale-in) scale_in_nodes ;;
    reconcile-target)
      # The operator may pass the generated target file itself as --config on
      # resume/finalize. GNU/BSD cp rejects a source copied onto itself; an
      # identical active config already represents the desired durable state.
      persist_active_config "$TARGET_CONFIG"
      kubespray_checkpoint="$STATE_DIR/finalize-reconcile-kubespray.done"
      if [[ -f "$kubespray_checkpoint" ]]; then
        # Rebuild normal role facts and replay lightweight convergence while
        # skipping only the expensive, already-proven Kubespray deployment.
        # --start-at-task is deliberately avoided because it also skips the
        # infrastructure facts required by the kubeconfig/tunnel tasks.
        run_playbook "$TARGET_CONFIG" -e hetzner_allow_destructive_reconcile=true \
          -e skip_kubespray=true
      else
        run_playbook "$TARGET_CONFIG" -e hetzner_allow_destructive_reconcile=true \
          -e "profile_migration_kubespray_checkpoint=$kubespray_checkpoint"
      fi
      reconcile_control_plane_schedulability "$TARGET_CONFIG"
      ;;
    final-backup)
      # The steady target may intentionally disable scheduled backups. Restore
      # the already-generated temporary backup control plane for this gate,
      # then retire it in the following checkpoint.
      validate_volume_capacity_settings
      check_volume_capacity
      run_playbook "$POST_BACKUP_CONFIG" --tags databases,gitlab,backup
      cluster_backup "$POST_BACKUP_CONFIG"
      ;;
    retire-backup) remove_disabled_components true ;;
    cleanup-cloud)
      if ! requires_spread "$TARGET_CONFIG" && hcloud placement-group describe "${PROJECT}-spread" >/dev/null 2>&1; then
        hcloud placement-group delete "${PROJECT}-spread"
      fi
      ;;
    validate-final)
      retry_gate "final node readiness" kubectl wait nodes --all --for=condition=Ready --timeout=900s
      check_etcd_health
      check_control_plane_schedulability_contract "$TARGET_CONFIG"
      retry_gate "final platform health" check_platform_health "$TARGET_CONFIG"
      ;;
  esac
}

if [[ "$COMMAND" == finalize ]]; then
  status=$(jq -r '.status' "$STATE_FILE")
  [[ "$status" == completed || "$status" == finalizing || "$status" == finalized ]] || fail "migration must complete before finalization"
  [[ -f "$STATE_DIR/stage-post-backup.done" ]] || fail "post-migration backup checkpoint is missing"
  [[ "$status" != finalized ]] || { log "migration finalization is already complete"; exit 0; }
  if [[ "$DRY_RUN" == true ]]; then
    for stage in "${FINALIZE_STAGES[@]}"; do [[ -f "$STATE_DIR/finalize-${stage}.done" ]] || dry "would run finalize stage: $stage"; done
    dry "would remove components: $(components_to_remove | paste -sd, - || true)"
    dry "would remove excess workers $(yq -r '.infrastructure.workers.count' "$EXPANSION_CONFIG")->$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG") and control planes $(yq -r '.infrastructure.control_plane.count' "$EXPANSION_CONFIG")->$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")"
    exit 0
  fi
  [[ -n "$BACKUP_RECIPIENT" || -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] || fail "set --backup-recipient or CLUSTER_BACKUP_PASSPHRASE before finalization"
  if [[ "$FORCE" != true ]]; then
    printf 'Type FINALIZE to retire source resources for %s -> %s: ' "$SOURCE_PROFILE" "$TARGET_PROFILE"
    read -r confirmation; [[ "$confirmation" == FINALIZE ]] || fail "confirmation did not match FINALIZE"
  fi
  jq '.status="finalizing" | .finalization_started_at=(.finalization_started_at // (now | todateiso8601))' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"; mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
  for stage in "${FINALIZE_STAGES[@]}"; do
    [[ -f "$STATE_DIR/finalize-${stage}.done" ]] && { log "finalize checkpoint already complete: $stage"; continue; }
    log "starting finalize stage: $stage"; finalize_stage "$stage"; mark_finalize_stage "$stage"
  done
  # A caller may resume with a generated state config as --config. Always
  # restore the durable active selector recorded when execution began (or the
  # canonical selector for legacy state) so the finalized target can be the
  # source of the next all-to-all transition.
  persist_active_config "$TARGET_CONFIG"
  jq '.status="finalized" | .finalized_at=(now | todateiso8601)' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"; mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
  log "${SOURCE_PROFILE} -> ${TARGET_PROFILE} finalized; obsolete services, PVCs, nodes, and cloud placement resources are retired"
  exit 0
fi

if [[ "$DRY_RUN" == true ]]; then
  for stage in "${STAGES[@]}"; do [[ -f "$STATE_DIR/stage-${stage}.done" ]] || dry "would run stage: $stage"; done
  exit 0
fi
if [[ "$COMMAND" == resume && -f "$STATE_DIR/stage-preflight.done" \
  && ! -f "$STATE_DIR/stage-post-backup.done" ]]; then
  check_volume_capacity
fi
for stage in "${STAGES[@]}"; do
  [[ -f "$STATE_DIR/stage-${stage}.done" ]] && { log "checkpoint already complete: $stage"; continue; }
  log "starting stage: $stage"
  case "$stage" in
    preflight) stage_preflight ;; backup) stage_backup ;; expand) stage_expand ;; resize) stage_resize ;;
    migrate-vault-storage) stage_migrate_vault_storage ;;
    apply-target) stage_apply_target ;; migrate-data) stage_migrate_data ;; validate) stage_validate ;; post-backup) stage_post_backup ;;
  esac
  mark_stage "$stage"
done
jq '.status="completed" | .completed_at=(now | todateiso8601)' "$STATE_FILE" > "${STATE_FILE}.tmp.$$"; mv "${STATE_FILE}.tmp.$$" "$STATE_FILE"
log "${SOURCE_PROFILE} -> ${TARGET_PROFILE} execution completed; run finalize after acceptance to retire source resources"
