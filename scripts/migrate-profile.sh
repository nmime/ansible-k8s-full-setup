#!/usr/bin/env bash
# Resumable, backup-gated migration between every named platform profile.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
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
RESIZE_TIMEOUT="${PROFILE_MIGRATION_RESIZE_TIMEOUT:-900s}"
VMCTL_IMAGE="docker.io/victoriametrics/vmctl:v1.147.0"
NAMED_PROFILES=(minimal small medium medium-optimized production)
STAGES=(preflight backup expand resize apply-target migrate-data validate post-backup)
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
for tool in yq jq ansible-playbook; do command -v "$tool" >/dev/null || fail "required tool is missing: $tool"; done

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
ROLLBACK_CONFIG="${STATE_DIR}/rollback-platform.yaml"
STORAGE_RETENTION_FILE="${STATE_DIR}/storage-retention.tsv"
STATEFUL_RETENTION_FILE="${STATE_DIR}/stateful-retention.tsv"

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
    glitchtip) printf '.glitchtip.enabled' ;; apm) printf '.apm.enabled' ;; blackbox) printf '.blackbox.enabled' ;;
    daytona) printf '.applications.daytona.enabled' ;; hipaa) printf '.compliance.hipaa.enabled' ;; *) return 1 ;;
  esac
}

component_enabled() {
  local file="$1" component="$2" path
  path=$(component_path "$component")
  [[ $(yq -r "${path} // false" "$file") == true ]]
}

components_to_remove() {
  local component
  for component in daytona hipaa blackbox apm glitchtip temporal postal tracing coroot gitlab-runner gitlab mongodb eso elasticsearch dragonfly backup autoscaling gitops observability postgresql databases secrets object-storage; do
    if component_enabled "$SOURCE_CONFIG" "$component" && ! component_enabled "$TARGET_CONFIG" "$component"; then printf '%s\n' "$component"; fi
  done
}

preserve_non_shrinking_storage() {
  : > "$STORAGE_RETENTION_FILE"
  : > "$STATEFUL_RETENTION_FILE"
  retain_larger_quantity seaweedfs-volume '.storage.size_per_replica // .storage.size' '.storage.size_per_replica // .storage.size' '.storage.size_per_replica' 50Gi 50Gi
  retain_larger_quantity seaweedfs-master '.storage.master_size' '.storage.master_size' '.storage.master_size' 4Gi 4Gi
  retain_larger_quantity seaweedfs-index '.storage.index_size' '.storage.index_size' '.storage.index_size' 2Gi 2Gi
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
  fi
}

generate_configs() {
  cp "$CONFIG_FILE" "$SOURCE_CONFIG"
  cp "$PROJECT_ROOT/platform-orchestrator/profiles/${TARGET_PROFILE}.yaml" "$TARGET_CONFIG"
  set_yaml_string "$TARGET_CONFIG" '.global.project' "$PROJECT"
  set_yaml_string "$TARGET_CONFIG" '.global.domain' "$DOMAIN"
  set_yaml_string "$TARGET_CONFIG" '.global.email' "$EMAIL"
  set_yaml_string "$TARGET_CONFIG" '.global.timezone' "$(yq -r '.global.timezone // "UTC"' "$SOURCE_CONFIG")"
  set_yaml_string "$TARGET_CONFIG" '.infrastructure.region' "$(yq -r '.infrastructure.region // "hel1"' "$SOURCE_CONFIG")"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"
  preserve_non_shrinking_storage

  source_cp=$(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG")
  source_workers=$(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG")
  target_cp=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  target_workers=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  (( source_cp > target_cp )) && transition_cp=$source_cp || transition_cp=$target_cp
  (( source_workers > target_workers )) && transition_workers=$source_workers || transition_workers=$target_workers

  cp "$SOURCE_CONFIG" "$BACKUP_CONFIG"
  yq -i '.platform_profile = "custom" | .backup.enabled = true | .backup.disaster_recovery.enabled = true' "$BACKUP_CONFIG"
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
EOF
  [[ ! -s "$STORAGE_RETENTION_FILE" ]] || { printf '\nRetained storage requests (source, requested target, YAML path):\n'; sed 's/\t/  /g' "$STORAGE_RETENTION_FILE"; }
  [[ ! -s "$STATEFUL_RETENTION_FILE" ]] || { printf '\nRetained data-bearing replicas (source, requested target, YAML path):\n'; sed 's/\t/  /g' "$STATEFUL_RETENTION_FILE"; }
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
  if [[ "$FORCE" != true ]]; then
    printf 'Type MIGRATE to start %s -> %s migration for %s: ' "$SOURCE_PROFILE" "$TARGET_PROFILE" "$PROJECT"
    read -r confirmation; [[ "$confirmation" == MIGRATE ]] || fail "confirmation did not match MIGRATE"
  fi
  jq -n --arg project "$PROJECT" --arg source "$SOURCE_PROFILE" --arg target "$TARGET_PROFILE" \
    --argjson sourceCp "$(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG")" \
    --argjson sourceWorkers "$(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG")" \
    --argjson targetCp "$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")" \
    --argjson targetWorkers "$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")" \
    '{schema_version:2,project:$project,source_profile:$source,target_profile:$target,status:"in_progress",
      created_at:(now | todateiso8601),last_completed_stage:null,
      topology:{source:{control_planes:$sourceCp,workers:$sourceWorkers},target:{control_planes:$targetCp,workers:$targetWorkers}}}' > "$STATE_FILE"
  printf '%s\n' "$STATE_DIR" > "$POINTER_FILE"
elif [[ "$COMMAND" == resume ]]; then
  for generated in "$SOURCE_CONFIG" "$TARGET_CONFIG" "$STEADY_CONFIG" "$EXPANSION_CONFIG" "$BACKUP_CONFIG" "$ROLLBACK_CONFIG"; do
    [[ -f "$generated" ]] || fail "migration config is missing: $generated"
  done
  case $(jq -r '.status' "$STATE_FILE") in
    completed|finalized) log "migration has no pending execution checkpoints"; exit 0 ;;
    rolled_back) fail "migration was rolled back; start a new execute workflow" ;;
  esac
fi

run_playbook() {
  local config="$1"; shift
  ansible-playbook "$PROJECT_ROOT/playbooks/deploy_platform.yml" -e "@$config" \
    -e "project_name=$PROJECT" -e "domain=$DOMAIN" -e "email=$EMAIL" "$@"
}

check_platform_health() {
  local config="$1" require_argocd require_postgresql require_mongodb workload_failures helm_failures cert_json not_ready_certificates pg_state mongo_state not_bound
  require_argocd=$(yq -r '.gitops.enabled // false' "$config")
  require_postgresql=$(yq -r '(.databases.enabled and .databases.postgresql.enabled) // false' "$config")
  require_mongodb=$(yq -r '(.databases.enabled and .databases.mongodb.enabled) // false' "$config")
  HEALTH_REQUIRE_ARGOCD="$require_argocd" HEALTH_REQUIRE_POSTGRESQL="$require_postgresql" HEALTH_REQUIRE_MONGODB="$require_mongodb" "$SCRIPT_DIR/health-gates.sh"
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
    [[ "${pg_state,,}" == ready ]] || fail "PostgreSQL operator state is ${pg_state:-missing}"
  fi
  if [[ "$require_mongodb" == true ]]; then
    mongo_state=$(kubectl get perconaservermongodb "${PROJECT}-mongo" -n databases -o jsonpath='{.status.state}')
    [[ "${mongo_state,,}" == ready ]] || fail "MongoDB operator state is ${mongo_state:-missing}"
  fi
  not_bound=$(kubectl get pvc -A -o json | jq '[.items[] | select(.status.phase != "Bound")] | length')
  [[ "$not_bound" == 0 ]] || fail "$not_bound PVCs are not Bound"
}

ssh_args_for_facts() {
  local facts="${PROJECT_ROOT}/${PROJECT}-infra-facts.yml"
  [[ -f "$facts" ]] || fail "infrastructure facts are missing: $facts"
  BASTION=$(yq -r '.bastion_public_ip' "$facts")
  SSH_ARGS=(-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=20 -J "root@${BASTION}")
}

check_etcd_health() {
  local excluded_node="${1:-}" facts="${PROJECT_ROOT}/${PROJECT}-infra-facts.yml" host
  ssh_args_for_facts
  if [[ -n "$excluded_node" ]]; then
    host=$(EXCLUDED_NODE="$excluded_node" yq -r '.master_ips | to_entries | map(select(.key != strenv(EXCLUDED_NODE))) | .[0].value' "$facts")
  else
    host=$(yq -r '.master_ips | to_entries | .[0].value' "$facts")
  fi
  [[ -n "$host" && "$host" != null ]] || fail "no healthy etcd peer is available for verification"
  ssh "${SSH_ARGS[@]}" "root@${host}" 'set -eu; . /etc/etcd.env; export ETCDCTL_API=3 ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl endpoint health --cluster'
}

mark_stage() {
  local stage="$1"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/stage-${stage}.done"
  jq --arg stage "$stage" '.last_completed_stage=$stage | .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

mark_finalize_stage() {
  local stage="$1"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/finalize-${stage}.done"
  jq --arg stage "$stage" '.last_completed_finalize_stage=$stage | .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

cluster_backup() {
  local config="$1"
  local args=(--config "$config" --output-dir "$STATE_DIR/backups" --force)
  [[ -z "$BACKUP_RECIPIENT" ]] || args+=(--recipient "$BACKUP_RECIPIENT")
  "$SCRIPT_DIR/cluster-backup.sh" "${args[@]}"
}

stage_preflight() {
  local tool expected actual
  for tool in kubectl helm hcloud ssh; do command -v "$tool" >/dev/null || fail "required live-migration tool is missing: $tool"; done
  [[ $(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE") == "$SOURCE_PROFILE" ]] || fail "active config no longer declares source profile $SOURCE_PROFILE"
  [[ -n "$DR_ENDPOINT" && "$DR_ENDPOINT" != *'.svc'* && "$DR_ENDPOINT" != *seaweedfs* ]] || fail "--dr-endpoint must be independent from the cluster"
  [[ -n "$DR_BUCKET" ]] || fail "--dr-bucket is required"
  [[ -n "${BACKUP_DR_ACCESS_KEY:-}" && -n "${BACKUP_DR_SECRET_KEY:-}" ]] || fail "BACKUP_DR_ACCESS_KEY and BACKUP_DR_SECRET_KEY are required"
  [[ -n "$BACKUP_RECIPIENT" || -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] || fail "set --backup-recipient or CLUSTER_BACKUP_PASSPHRASE"
  [[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required"
  validate_generated_configs
  kubectl cluster-info >/dev/null
  expected=$(( $(yq -r '.infrastructure.control_plane.count' "$SOURCE_CONFIG") + $(yq -r '.infrastructure.workers.count' "$SOURCE_CONFIG") ))
  actual=$(kubectl get nodes -o json | jq '.items | length')
  [[ "$actual" == "$expected" ]] || fail "source profile declares $expected nodes but cluster has $actual"
  check_platform_health "$SOURCE_CONFIG"
  check_etcd_health
  hcloud server describe "${PROJECT}-master-1" >/dev/null
}

stage_backup() {
  run_playbook "$BACKUP_CONFIG" --tags backup
  mkdir -p "$STATE_DIR/backups"
  cluster_backup "$BACKUP_CONFIG"
  # shellcheck disable=SC1091
  source "$SCRIPT_DIR/snapshot-helm-baseline.sh"
  export SNAPSHOT_DRY_RUN=false
  snapshot=$(capture_snapshot | tail -1)
  jq --arg snapshot "$snapshot" '.helm_snapshot=$snapshot' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

requires_spread() { case $(profile_tier "$1") in medium|production) return 0 ;; *) return 1 ;; esac; }

stage_expand() {
  local expected actual
  if requires_spread "$SOURCE_CONFIG" || requires_spread "$TARGET_CONFIG"; then
    hcloud placement-group describe "${PROJECT}-spread" >/dev/null 2>&1 || hcloud placement-group create --name "${PROJECT}-spread" --type spread
  fi
  run_playbook "$EXPANSION_CONFIG" --tags infrastructure,cluster
  expected=$(( $(yq -r '.infrastructure.control_plane.count' "$EXPANSION_CONFIG") + $(yq -r '.infrastructure.workers.count' "$EXPANSION_CONFIG") ))
  kubectl wait nodes --all --for=condition=Ready --timeout=900s
  actual=$(kubectl get nodes -o json | jq '.items | length')
  [[ "$actual" == "$expected" ]] || fail "expected $expected Kubernetes nodes after expansion, found $actual"
  check_etcd_health
}

resize_node() {
  local node="$1" target_type="$2" role="$3" current_type placement server_status target_placement=""
  server_json=$(hcloud server describe "$node" -o json)
  current_type=$(jq -r '.server_type.name' <<<"$server_json")
  placement=$(jq -r '.placement_group.name // ""' <<<"$server_json")
  server_status=$(jq -r '.status' <<<"$server_json")
  requires_spread "$TARGET_CONFIG" && target_placement="${PROJECT}-spread"
  if [[ "$current_type" == "$target_type" && "$placement" == "$target_placement" ]]; then
    [[ "$server_status" != off ]] || hcloud server poweron "$node"
    kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
    kubectl uncordon "$node" >/dev/null
    [[ "$role" != master ]] || check_etcd_health
    log "$node already converged at type=$target_type"
    return 0
  fi
  [[ "$role" != master ]] || check_etcd_health "$node"
  kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=15m
  hcloud server poweroff "$node"
  if [[ -n "$target_placement" && "$placement" != "$target_placement" ]]; then
    [[ -z "$placement" ]] || hcloud server remove-from-placement-group "$node"
    hcloud server add-to-placement-group --placement-group "$target_placement" "$node"
  elif [[ -z "$target_placement" && -n "$placement" ]]; then
    hcloud server remove-from-placement-group "$node"
  fi
  [[ "$current_type" == "$target_type" ]] || hcloud server change-type --keep-disk "$node" "$target_type"
  hcloud server poweron "$node"
  kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  kubectl uncordon "$node"
  [[ "$role" != master ]] || check_etcd_health
}

stage_resize() {
  local workers masters cp_type worker_type i
  workers=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  masters=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  worker_type=$(yq -r '.infrastructure.workers.type' "$TARGET_CONFIG")
  cp_type=$(yq -r '.infrastructure.control_plane.type' "$TARGET_CONFIG")
  for ((i=1; i<=workers; i++)); do resize_node "${PROJECT}-worker-${i}" "$worker_type" worker; done
  for ((i=2; i<=masters; i++)); do resize_node "${PROJECT}-master-${i}" "$cp_type" master; done
  resize_node "${PROJECT}-master-1" "$cp_type" master
}

stage_apply_target() {
  [[ -f "$STATE_DIR/pre-target-platform.yaml" ]] || cp "$CONFIG_FILE" "$STATE_DIR/pre-target-platform.yaml"
  switched_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  jq --arg switched "$switched_at" '.target_write_switch_started_at=$switched' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
  cp "$STEADY_CONFIG" "$CONFIG_FILE"
  run_playbook "$CONFIG_FILE" --skip-tags infrastructure
}

run_vmctl_migration() {
  local job="$1" source_addr="$2" target_addr="$3" time_start="${4:-}" phase
  job=$(printf '%s' "$job" | tr '[:upper:]_' '[:lower:]-' | cut -c1-63)
  if kubectl get job "$job" -n monitoring >/dev/null 2>&1; then
    phase=$(kubectl get job "$job" -n monitoring -o json | jq -r '
      if ([.status.conditions[]? | select(.type == "Complete" and .status == "True")] | length) > 0 then "complete"
      elif ([.status.conditions[]? | select(.type == "Failed" and .status == "True")] | length) > 0 then "failed"
      else "running" end')
    [[ "$phase" != failed ]] || fail "VictoriaMetrics job $job failed; inspect it and restore the destination before retrying to avoid duplicate samples"
    if [[ "$phase" == complete ]]; then log "VictoriaMetrics job already complete: $job"; return 0; fi
  else
    time_arg=""
    [[ -z "$time_start" ]] || time_arg="            - --vm-native-filter-time-start=${time_start}"
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
      restartPolicy: Never
      containers:
        - name: vmctl
          image: ${VMCTL_IMAGE}
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
  check_platform_health "$STEADY_CONFIG"
  kubectl get backupstoragelocation/default -n velero -o json | jq -e '.status.phase == "Available"' >/dev/null
}

stage_post_backup() { cluster_backup "$STEADY_CONFIG"; }

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
  "$SCRIPT_DIR/rollback.sh" --snapshot "$snapshot" --force
  cp "$ROLLBACK_CONFIG" "$CONFIG_FILE"
  run_playbook "$ROLLBACK_CONFIG" --skip-tags infrastructure
  jq '.status="rolled_back" | .rolled_back_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"
  log "source capabilities restored; expanded or resized nodes were deliberately retained"
  exit 0
fi

remove_disabled_components() {
  local include_backup="$1" component
  while IFS= read -r component; do
    [[ "$component" == backup ]] && [[ "$include_backup" != true ]] && continue
    [[ "$component" != backup ]] && [[ "$include_backup" == true ]] && continue
    ansible-playbook "$PROJECT_ROOT/playbooks/remove_component.yml" -e "@$TARGET_CONFIG" -e "project_name=$PROJECT" \
      -e "target_component=$component" -e "confirm_component_removal=$component" -e delete_component_data=true
  done < <(components_to_remove)
}

capture_observability_pvcs() {
  local pattern="$1"
  kubectl get pods -n monitoring -o json | jq -r --arg pattern "$pattern" '
    .items[] | select(.metadata.name | test($pattern)) | .spec.volumes[]? | .persistentVolumeClaim.claimName // empty' | sort -u
}

retire_observability_source() {
  local source_mode target_mode pvc release pvc_file="${STATE_DIR}/retire-observability-pvcs.txt"
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
    for release in promtail loki; do helm status "$release" -n monitoring >/dev/null 2>&1 && helm uninstall "$release" -n monitoring --wait --timeout 10m0s; done
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
    -i "$inventory" --become --become-user=root "$kubespray_dir/remove-node.yml" -e "node=$node"
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
      cp "$TARGET_CONFIG" "$CONFIG_FILE"
      run_playbook "$TARGET_CONFIG" -e hetzner_allow_destructive_reconcile=true
      ;;
    final-backup) cluster_backup "$TARGET_CONFIG" ;;
    retire-backup) remove_disabled_components true ;;
    cleanup-cloud)
      if ! requires_spread "$TARGET_CONFIG" && hcloud placement-group describe "${PROJECT}-spread" >/dev/null 2>&1; then
        hcloud placement-group delete "${PROJECT}-spread"
      fi
      ;;
    validate-final)
      kubectl wait nodes --all --for=condition=Ready --timeout=900s
      check_etcd_health
      check_platform_health "$TARGET_CONFIG"
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
  jq '.status="finalizing" | .finalization_started_at=(.finalization_started_at // (now | todateiso8601))' "$STATE_FILE" > "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"
  for stage in "${FINALIZE_STAGES[@]}"; do
    [[ -f "$STATE_DIR/finalize-${stage}.done" ]] && { log "finalize checkpoint already complete: $stage"; continue; }
    log "starting finalize stage: $stage"; finalize_stage "$stage"; mark_finalize_stage "$stage"
  done
  jq '.status="finalized" | .finalized_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"
  log "${SOURCE_PROFILE} -> ${TARGET_PROFILE} finalized; obsolete services, PVCs, nodes, and cloud placement resources are retired"
  exit 0
fi

if [[ "$DRY_RUN" == true ]]; then
  for stage in "${STAGES[@]}"; do [[ -f "$STATE_DIR/stage-${stage}.done" ]] || dry "would run stage: $stage"; done
  exit 0
fi
for stage in "${STAGES[@]}"; do
  [[ -f "$STATE_DIR/stage-${stage}.done" ]] && { log "checkpoint already complete: $stage"; continue; }
  log "starting stage: $stage"
  case "$stage" in
    preflight) stage_preflight ;; backup) stage_backup ;; expand) stage_expand ;; resize) stage_resize ;;
    apply-target) stage_apply_target ;; migrate-data) stage_migrate_data ;; validate) stage_validate ;; post-backup) stage_post_backup ;;
  esac
  mark_stage "$stage"
done
jq '.status="completed" | .completed_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"; mv "$STATE_FILE.tmp" "$STATE_FILE"
log "${SOURCE_PROFILE} -> ${TARGET_PROFILE} execution completed; run finalize after acceptance to retire source resources"
