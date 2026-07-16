#!/usr/bin/env bash
# Resumable, backup-gated in-place platform profile migration.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG_FILE="${PROJECT_ROOT}/platform-orchestrator/platform.yaml"
TARGET_PROFILE=production
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

log() { printf '[profile-migration] %s\n' "$*"; }
warn() { printf '[profile-migration] WARNING: %s\n' "$*" >&2; }
fail() { printf '[profile-migration] ERROR: %s\n' "$*" >&2; exit 1; }
dry() { printf '[profile-migration] DRY-RUN: %s\n' "$*"; }

usage() {
  cat <<'EOF'
Usage: migrate-profile.sh [OPTIONS] <command>

Commands:
  plan       Generate and validate migration configurations without a cluster change
  execute    Start migration; refuses an existing incomplete migration
  resume     Continue from the first incomplete checkpoint
  status     Show durable checkpoint state
  rollback   Restore pre-migration application config; expanded nodes are retained
  finalize   After sign-off, remove superseded VMSingle/Loki compute and PVCs

Options:
  --config FILE          Active platform YAML
  --target PROFILE       Target profile (currently: production)
  --dr-endpoint URL      External S3-compatible Velero endpoint
  --dr-bucket NAME       External disaster-recovery bucket
  --dr-region NAME       S3 region (default: us-east-1)
  --dr-prefix PREFIX     Velero prefix (default: <project>/velero)
  --backup-recipient ID  age recipient for encrypted cluster bundles
  --dry-run              Show every stage without mutation
  --force                Skip interactive MIGRATE/FINALIZE confirmations
  -h, --help             Show this help

The workflow expands the control plane and workers before resizing anything,
drains and resizes one node at a time, verifies etcd after every control-plane
change, reconciles the production profile, migrates VMSingle history into
VMCluster, and takes another full backup. It never automatically deletes nodes.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="$2"; shift 2 ;;
    --target) TARGET_PROFILE="$2"; shift 2 ;;
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
[[ "$TARGET_PROFILE" == production ]] || fail "the only verified migration target is production"

for tool in yq jq ansible-playbook; do command -v "$tool" >/dev/null || fail "required tool is missing: $tool"; done
SOURCE_PROFILE=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
[[ "$SOURCE_PROFILE" == minimal || "$COMMAND" == status || "$COMMAND" == resume || "$COMMAND" == rollback || "$COMMAND" == finalize ]] || \
  fail "the verified path is minimal -> production; current profile is $SOURCE_PROFILE"
PROJECT=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
DOMAIN=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
EMAIL=$(yq -r '.global.email // ""' "$CONFIG_FILE")
[[ -n "$DR_PREFIX" ]] || DR_PREFIX="${PROJECT}/velero"
STATE_BASE="${PROFILE_MIGRATION_STATE_DIR:-${PROJECT_ROOT}/.migration-state}"
STATE_DIR="${STATE_BASE}/${PROJECT}-minimal-to-production"
STATE_FILE="${STATE_DIR}/state.json"
TARGET_CONFIG="${STATE_DIR}/target-platform.yaml"
EXPANSION_CONFIG="${STATE_DIR}/expansion-platform.yaml"
BACKUP_CONFIG="${STATE_DIR}/backup-platform.yaml"
ROLLBACK_CONFIG="${STATE_DIR}/rollback-platform.yaml"
STAGES=(preflight backup expand resize apply-production migrate-data validate post-backup)

state_status() {
  if [[ ! -f "$STATE_FILE" ]]; then log "no migration state for $PROJECT"; return 0; fi
  jq . "$STATE_FILE"
  for stage in "${STAGES[@]}"; do
    if [[ -f "$STATE_DIR/stage-${stage}.done" ]]; then printf '  %-18s complete\n' "$stage"; else printf '  %-18s pending\n' "$stage"; fi
  done
}
[[ "$COMMAND" != status ]] || { state_status; exit 0; }

mkdir -p "$STATE_DIR"
chmod 700 "$STATE_DIR"

set_yaml_string() {
  local file="$1" path="$2" value="$3"
  VALUE="$value" yq -i "${path} = strenv(VALUE)" "$file"
}

generate_configs() {
  cp "$PROJECT_ROOT/platform-orchestrator/profiles/production.yaml" "$TARGET_CONFIG"
  set_yaml_string "$TARGET_CONFIG" '.global.project' "$PROJECT"
  set_yaml_string "$TARGET_CONFIG" '.global.domain' "$DOMAIN"
  set_yaml_string "$TARGET_CONFIG" '.global.email' "$EMAIL"
  current_timezone=$(yq -r '.global.timezone // "UTC"' "$CONFIG_FILE")
  set_yaml_string "$TARGET_CONFIG" '.global.timezone' "$current_timezone"
  current_region=$(yq -r '.infrastructure.region // "hel1"' "$CONFIG_FILE")
  set_yaml_string "$TARGET_CONFIG" '.infrastructure.region' "$current_region"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$TARGET_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"

  cp "$CONFIG_FILE" "$BACKUP_CONFIG"
  yq -i '.platform_profile = "custom" | .backup.enabled = true | .backup.disaster_recovery.enabled = true' "$BACKUP_CONFIG"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.endpoint' "$DR_ENDPOINT"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.region' "$DR_REGION"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.bucket' "$DR_BUCKET"
  set_yaml_string "$BACKUP_CONFIG" '.backup.disaster_recovery.prefix' "$DR_PREFIX"
  yq -i '.backup.disaster_recovery.schedule = "30 2 * * *" | .backup.disaster_recovery.retention_hours = 720' "$BACKUP_CONFIG"

  cp "$CONFIG_FILE" "$EXPANSION_CONFIG"
  yq -i '.platform_profile = "custom" | .kubernetes.ha_control_plane = true' "$EXPANSION_CONFIG"
  target_cp_count=$(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
  target_worker_count=$(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
  TARGET_CP_COUNT="$target_cp_count" TARGET_WORKER_COUNT="$target_worker_count" \
    yq -i '.infrastructure.control_plane.count = (strenv(TARGET_CP_COUNT) | tonumber) | .infrastructure.workers.count = (strenv(TARGET_WORKER_COUNT) | tonumber)' "$EXPANSION_CONFIG"

  cp "$EXPANSION_CONFIG" "$ROLLBACK_CONFIG"
  target_cp_type=$(yq -r '.infrastructure.control_plane.type' "$TARGET_CONFIG")
  target_worker_type=$(yq -r '.infrastructure.workers.type' "$TARGET_CONFIG")
  set_yaml_string "$ROLLBACK_CONFIG" '.infrastructure.control_plane.type' "$target_cp_type"
  set_yaml_string "$ROLLBACK_CONFIG" '.infrastructure.workers.type' "$target_worker_type"
}

validate_generated_configs() {
  ansible-playbook "$PROJECT_ROOT/playbooks/validate_profile.yml" -e "@$TARGET_CONFIG" >/dev/null
  ansible-playbook "$PROJECT_ROOT/playbooks/validate_profile.yml" -e "@$BACKUP_CONFIG" >/dev/null
  ansible-playbook "$PROJECT_ROOT/playbooks/validate_profile.yml" -e "@$EXPANSION_CONFIG" >/dev/null
}

if [[ "$COMMAND" == execute && -f "$STATE_FILE" ]]; then
  if [[ $(jq -r '.status' "$STATE_FILE") != rolled_back ]]; then
    fail "migration state already exists; use resume, status, rollback, or finalize"
  fi
  archived_state="${STATE_DIR}-$(date -u +%Y%m%dT%H%M%SZ)"
  mv "$STATE_DIR" "$archived_state"
  mkdir -p "$STATE_DIR"
  chmod 700 "$STATE_DIR"
fi

if [[ "$COMMAND" == plan || "$COMMAND" == execute ]]; then
  generate_configs
elif [[ "$COMMAND" == resume ]]; then
  [[ -f "$STATE_FILE" && -f "$TARGET_CONFIG" && -f "$EXPANSION_CONFIG" \
    && -f "$BACKUP_CONFIG" && -f "$ROLLBACK_CONFIG" ]] \
    || fail "migration state/configs are incomplete; restore the state directory from backup"
  case $(jq -r '.status' "$STATE_FILE") in
    completed|finalized) log "migration has no pending checkpoints"; exit 0 ;;
    rolled_back) fail "migration was rolled back; start a new execute workflow" ;;
  esac
fi
if [[ "$COMMAND" == plan ]]; then
  validate_generated_configs
  log "validated minimal -> production migration plan"
  diff -u "$CONFIG_FILE" "$TARGET_CONFIG" || true
  cat <<EOF

Stages: ${STAGES[*]}
Control planes: $(yq -r '.infrastructure.control_plane.count' "$CONFIG_FILE") -> $(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG")
Workers: $(yq -r '.infrastructure.workers.count' "$CONFIG_FILE") -> $(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG")
External backup endpoint: ${DR_ENDPOINT:-MISSING}
External backup bucket: ${DR_BUCKET:-MISSING}
EOF
  exit 0
fi

if [[ "$COMMAND" == rollback ]]; then
  [[ -f "$STATE_FILE" ]] || fail "no migration state exists"
  [[ -f "$ROLLBACK_CONFIG" ]] || fail "rollback configuration is missing"
  if [[ $(jq -r '.status' "$STATE_FILE") == rolled_back ]]; then
    log "migration is already rolled back"
    exit 0
  fi
  [[ $(jq -r '.status' "$STATE_FILE") != finalized ]] \
    || fail "source data was finalized; restore the pre-finalize recovery bundle before a minimal-profile rollback"
  if [[ "$DRY_RUN" == true ]]; then
    dry "would roll Helm releases back to the recorded baseline"
    dry "would activate the minimal-capability custom profile while retaining expanded nodes"
    exit 0
  fi
  snapshot=$(jq -r '.helm_snapshot // ""' "$STATE_FILE")
  [[ -n "$snapshot" && -d "$snapshot" ]] || fail "recorded Helm snapshot is unavailable"
  "$SCRIPT_DIR/rollback.sh" --snapshot "$snapshot" --force
  cp "$ROLLBACK_CONFIG" "$CONFIG_FILE"
  jq '.status="rolled_back" | .rolled_back_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
  log "application configuration rolled back; expanded nodes were deliberately retained"
  exit 0
fi

mark_stage() {
  local stage="$1"
  date -u +%Y-%m-%dT%H:%M:%SZ > "$STATE_DIR/stage-${stage}.done"
  jq --arg stage "$stage" '.last_completed_stage=$stage | .updated_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

run_playbook() {
  local config="$1"; shift
  ansible-playbook "$PROJECT_ROOT/playbooks/deploy_platform.yml" \
    -e "@$config" \
    -e "project_name=$PROJECT" -e "domain=$DOMAIN" -e "email=$EMAIL" "$@"
}

check_platform_health() {
  local require_argocd require_postgresql require_mongodb workload_failures helm_failures
  local cert_json not_ready_certificates not_bound pg_state mongo_state
  require_argocd=$(yq -r '.gitops.enabled // false' "$CONFIG_FILE")
  require_postgresql=$(yq -r '(.databases.enabled and .databases.postgresql.enabled) // false' "$CONFIG_FILE")
  require_mongodb=$(yq -r '(.databases.enabled and .databases.mongodb.enabled) // false' "$CONFIG_FILE")
  HEALTH_REQUIRE_ARGOCD="$require_argocd" \
    HEALTH_REQUIRE_POSTGRESQL="$require_postgresql" \
    HEALTH_REQUIRE_MONGODB="$require_mongodb" \
    "$SCRIPT_DIR/health-gates.sh"

  workload_failures=$(kubectl get deployments,statefulsets,daemonsets -A -o json | jq '[
    .items[]
    | select(
        (.kind == "Deployment" and
          ((.status.readyReplicas // 0) < (.spec.replicas // 1) or
           (.status.updatedReplicas // 0) < (.spec.replicas // 1)))
        or (.kind == "StatefulSet" and
          ((.status.readyReplicas // 0) < (.spec.replicas // 1) or
           (((.spec.updateStrategy.type // "RollingUpdate") != "OnDelete") and
            (.status.updatedReplicas // 0) < (.spec.replicas // 1))))
        or (.kind == "DaemonSet" and
          ((.status.numberReady // 0) < (.status.desiredNumberScheduled // 0) or
           (((.spec.updateStrategy.type // "RollingUpdate") != "OnDelete") and
            (.status.updatedNumberScheduled // 0) < (.status.desiredNumberScheduled // 0))))
      )
  ] | length')
  [[ "$workload_failures" == 0 ]] || fail "$workload_failures controller workloads are not fully rolled out"

  helm_failures=$(helm list --all-namespaces --output json | jq '[.[] | select(.status != "deployed")] | length')
  [[ "$helm_failures" == 0 ]] || fail "$helm_failures active Helm releases are not deployed"

  if cert_json=$(kubectl get certificates --all-namespaces -o json 2>/dev/null); then
    not_ready_certificates=$(jq '[
      .items[]
      | select([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 0)
    ] | length' <<<"$cert_json")
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
  ssh "${SSH_ARGS[@]}" "root@${host}" \
    'set -eu; . /etc/etcd.env; export ETCDCTL_API=3 ETCDCTL_ENDPOINTS ETCDCTL_CACERT ETCDCTL_CERT ETCDCTL_KEY; etcdctl endpoint health --cluster'
}

stage_preflight() {
  local tool
  for tool in kubectl hcloud ssh; do command -v "$tool" >/dev/null || fail "required live-migration tool is missing: $tool"; done
  [[ "$SOURCE_PROFILE" == minimal ]] || fail "resume source must still be minimal before production activation"
  [[ -n "$DR_ENDPOINT" && "$DR_ENDPOINT" != *'.svc'* && "$DR_ENDPOINT" != *seaweedfs* ]] || fail "--dr-endpoint must be external"
  [[ -n "$DR_BUCKET" ]] || fail "--dr-bucket is required"
  [[ -n "${BACKUP_DR_ACCESS_KEY:-}" && -n "${BACKUP_DR_SECRET_KEY:-}" ]] || fail "BACKUP_DR_ACCESS_KEY and BACKUP_DR_SECRET_KEY are required"
  [[ -n "$BACKUP_RECIPIENT" || -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] \
    || fail "set --backup-recipient or CLUSTER_BACKUP_PASSPHRASE for encrypted cluster bundles"
  [[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required"
  validate_generated_configs
  kubectl cluster-info >/dev/null
  check_platform_health
  hcloud server describe "${PROJECT}-master-1" >/dev/null
}

stage_backup() {
  local cluster_backup_args=(--config "$BACKUP_CONFIG" --output-dir "$STATE_DIR/backups" --force)
  [[ -z "$BACKUP_RECIPIENT" ]] || cluster_backup_args+=(--recipient "$BACKUP_RECIPIENT")
  run_playbook "$BACKUP_CONFIG" --tags backup
  mkdir -p "$STATE_DIR/backups"
  "$SCRIPT_DIR/cluster-backup.sh" "${cluster_backup_args[@]}"
  # shellcheck source=./scripts/snapshot-helm-baseline.sh
  source "$SCRIPT_DIR/snapshot-helm-baseline.sh"
  export SNAPSHOT_DRY_RUN=false
  snapshot=$(capture_snapshot | tail -1)
  jq --arg snapshot "$snapshot" '.helm_snapshot=$snapshot' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
}

stage_expand() {
  if ! hcloud placement-group describe "${PROJECT}-spread" >/dev/null 2>&1; then
    hcloud placement-group create --name "${PROJECT}-spread" --type spread
  fi
  run_playbook "$EXPANSION_CONFIG" --tags infrastructure,cluster
  expected=$(( $(yq -r '.infrastructure.control_plane.count' "$TARGET_CONFIG") + $(yq -r '.infrastructure.workers.count' "$TARGET_CONFIG") ))
  kubectl wait nodes --all --for=condition=Ready --timeout=900s
  actual=$(kubectl get nodes -o json | jq '.items | length')
  [[ "$actual" == "$expected" ]] || fail "expected $expected Kubernetes nodes after expansion, found $actual"
  check_etcd_health
}

resize_node() {
  local node="$1" target_type="$2" role="$3" current_type placement
  current_type=$(hcloud server describe "$node" -o json | jq -r '.server_type.name')
  placement=$(hcloud server describe "$node" -o json | jq -r '.placement_group.name // ""')
  if [[ "$current_type" == "$target_type" && "$placement" == "${PROJECT}-spread" ]]; then
    log "$node already has type $target_type and correct placement"
    return 0
  fi
  kubectl drain "$node" --ignore-daemonsets --delete-emptydir-data --timeout=15m
  hcloud server poweroff "$node"
  if [[ "$placement" != "${PROJECT}-spread" ]]; then
    hcloud server add-to-placement-group --placement-group "${PROJECT}-spread" "$node"
  fi
  [[ "$current_type" == "$target_type" ]] || hcloud server change-type --keep-disk "$node" "$target_type"
  hcloud server poweron "$node"
  kubectl wait "node/${node}" --for=condition=Ready --timeout="$RESIZE_TIMEOUT"
  kubectl uncordon "$node"
  [[ "$role" != master ]] || check_etcd_health "$node"
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

stage_apply_production() {
  cp "$CONFIG_FILE" "$STATE_DIR/pre-production-platform.yaml"
  cp "$TARGET_CONFIG" "$CONFIG_FILE"
  run_playbook "$CONFIG_FILE"
}

stage_migrate_data() {
  if kubectl get vmsingle vmsingle -n monitoring >/dev/null 2>&1 && kubectl get vmcluster vmcluster -n monitoring >/dev/null 2>&1; then
    job="vm-history-migration-$(date -u +%Y%m%d%H%M%S)"
    kubectl apply -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: ${job}
  namespace: monitoring
  labels:
    app.kubernetes.io/part-of: profile-migration
spec:
  backoffLimit: 1
  ttlSecondsAfterFinished: 86400
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: vmctl
          image: ${VMCTL_IMAGE}
          args:
            - vm-native
            - --vm-native-src-addr=http://vmsingle-vmsingle.monitoring.svc:8429
            - --vm-native-dst-addr=http://vminsert-vmcluster.monitoring.svc:8480
          resources:
            requests: {cpu: 100m, memory: 128Mi}
            limits: {cpu: "1", memory: 1Gi}
EOF
    if ! kubectl wait "job/${job}" -n monitoring --for=condition=complete --timeout=8h; then
      kubectl logs "job/${job}" -n monitoring --all-containers --tail=300 || true
      fail "VictoriaMetrics history migration failed"
    fi
  fi
  warn "the former Loki object-store data is retained as an archive; production writes use Elasticsearch"
}

stage_validate() {
  kubectl wait nodes --all --for=condition=Ready --timeout=900s
  check_etcd_health
  check_platform_health
  kubectl get backupstoragelocation/default -n velero -o json | jq -e '.status.phase == "Available"' >/dev/null
}

stage_post_backup() {
  local cluster_backup_args=(--config "$CONFIG_FILE" --output-dir "$STATE_DIR/backups" --force)
  [[ -z "$BACKUP_RECIPIENT" ]] || cluster_backup_args+=(--recipient "$BACKUP_RECIPIENT")
  "$SCRIPT_DIR/cluster-backup.sh" "${cluster_backup_args[@]}"
}

if [[ "$COMMAND" == finalize ]]; then
  [[ -f "$STATE_FILE" ]] || fail "no migration state exists"
  [[ $(jq -r '.status' "$STATE_FILE") == completed || $(jq -r '.status' "$STATE_FILE") == finalized ]] \
    || fail "migration must complete before source retirement"
  [[ -f "$STATE_DIR/stage-post-backup.done" ]] || fail "post-migration backup checkpoint is missing"
  if [[ $(jq -r '.status' "$STATE_FILE") == finalized ]]; then
    log "migration source retirement is already finalized"
    exit 0
  fi
  if [[ "$DRY_RUN" == true ]]; then
    dry "would remove the superseded VMSingle, Loki, and Promtail workloads and their exact PVCs"
    dry "would retain the Loki object-store archive and take another encrypted full-cluster backup"
    exit 0
  fi
  [[ -n "$BACKUP_RECIPIENT" || -n "${CLUSTER_BACKUP_PASSPHRASE:-}" ]] \
    || fail "set --backup-recipient or CLUSTER_BACKUP_PASSPHRASE before finalization"
  if [[ "$FORCE" != true ]]; then
    printf 'Type FINALIZE to retire superseded minimal-profile data stores for %s: ' "$PROJECT"
    read -r confirmation
    [[ "$confirmation" == FINALIZE ]] || fail "confirmation did not match FINALIZE"
  fi
  check_platform_health
  archived_pvcs=()
  while IFS= read -r pvc; do
    [[ -n "$pvc" ]] && archived_pvcs+=("$pvc")
  done < <(kubectl get pods -n monitoring -o json | jq -r '
    .items[]
    | select(.metadata.name | startswith("vmsingle-vmsingle-") or startswith("loki-"))
    | .spec.volumes[]?
    | .persistentVolumeClaim.claimName // empty' | sort -u)
  kubectl delete vmsingle vmsingle -n monitoring --ignore-not-found --wait --timeout=10m
  for release in promtail loki; do
    if helm status "$release" -n monitoring >/dev/null 2>&1; then
      helm uninstall "$release" -n monitoring --wait --timeout 10m0s
    fi
  done
  if (( ${#archived_pvcs[@]} > 0 )); then
    kubectl delete pvc -n monitoring "${archived_pvcs[@]}" --ignore-not-found --wait --timeout=10m
  fi
  check_platform_health
  cluster_backup_args=(--config "$CONFIG_FILE" --output-dir "$STATE_DIR/backups" --force)
  [[ -z "$BACKUP_RECIPIENT" ]] || cluster_backup_args+=(--recipient "$BACKUP_RECIPIENT")
  "$SCRIPT_DIR/cluster-backup.sh" "${cluster_backup_args[@]}"
  jq '.status="finalized" | .finalized_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
  mv "$STATE_FILE.tmp" "$STATE_FILE"
  log "superseded compute and PVCs retired; external Loki objects and recovery backups retained"
  exit 0
fi

if [[ ! -f "$STATE_FILE" || "$COMMAND" == execute ]]; then
  jq -n --arg project "$PROJECT" --arg source minimal --arg target production \
    '{schema_version:1,project:$project,source_profile:$source,target_profile:$target,
      status:"in_progress",created_at:(now | todateiso8601),last_completed_stage:null}' > "$STATE_FILE"
fi

if [[ "$DRY_RUN" == true ]]; then
  for stage in "${STAGES[@]}"; do [[ -f "$STATE_DIR/stage-${stage}.done" ]] || dry "would run stage: $stage"; done
  exit 0
fi
if [[ "$COMMAND" == execute && "$FORCE" != true ]]; then
  printf 'Type MIGRATE to start minimal -> production migration for %s: ' "$PROJECT"
  read -r confirmation
  [[ "$confirmation" == MIGRATE ]] || fail "confirmation did not match MIGRATE"
fi

for stage in "${STAGES[@]}"; do
  [[ -f "$STATE_DIR/stage-${stage}.done" ]] && { log "checkpoint already complete: $stage"; continue; }
  log "starting stage: $stage"
  case "$stage" in
    preflight) stage_preflight ;;
    backup) stage_backup ;;
    expand) stage_expand ;;
    resize) stage_resize ;;
    apply-production) stage_apply_production ;;
    migrate-data) stage_migrate_data ;;
    validate) stage_validate ;;
    post-backup) stage_post_backup ;;
  esac
  mark_stage "$stage"
done
jq '.status="completed" | .completed_at=(now | todateiso8601)' "$STATE_FILE" > "$STATE_FILE.tmp"
mv "$STATE_FILE.tmp" "$STATE_FILE"
log "minimal -> production migration completed with all checkpoints"
