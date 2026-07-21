#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${ROOT_DIR}/scripts/load-project-env.sh"
if ! command -v aws >/dev/null 2>&1 && [[ -x "${ROOT_DIR}/.venv/bin/aws" ]]; then
  PATH="${ROOT_DIR}/.venv/bin:${PATH}"
  export PATH
fi

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '  %s\n' "$*"; }

PROJECT="${1:-}"
[[ $# -gt 0 ]] && shift
CONFIRM=""
ACTIVE_MIGRATION_CONFIRM=""
API_LOCAL_PORT="${K8S_API_LOCAL_PORT:-16443}"
BACKUP_RECEIPT=""
MAX_BACKUP_AGE_SECONDS="${CLUSTER_TEARDOWN_MAX_BACKUP_AGE_SECONDS:-86400}"
TEARDOWN_GATE_DIR=""
cleanup_gate() { [[ -z "$TEARDOWN_GATE_DIR" ]] || rm -rf "$TEARDOWN_GATE_DIR"; }
trap cleanup_gate EXIT INT TERM

while [[ $# -gt 0 ]]; do
  case "$1" in
    --confirm) CONFIRM="${2:-}"; shift 2 ;;
    --confirm-active-migration) ACTIVE_MIGRATION_CONFIRM="${2:-}"; shift 2 ;;
    --api-port) API_LOCAL_PORT="${2:-}"; shift 2 ;;
    --require-backup-receipt) BACKUP_RECEIPT="${2:-}"; shift 2 ;;
    --max-backup-age-seconds) MAX_BACKUP_AGE_SECONDS="${2:-}"; shift 2 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -n "$PROJECT" ]] || fail "usage: $0 <project> --confirm <project> [--confirm-active-migration PHRASE] [--require-backup-receipt FILE] [--max-backup-age-seconds N] [--api-port PORT]"
[[ "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || fail "invalid project name"
[[ "$CONFIRM" == "$PROJECT" ]] || fail "confirmation must exactly match project '$PROJECT'"
[[ "$API_LOCAL_PORT" =~ ^[0-9]+$ ]] || fail "API tunnel port must be an integer"
(( API_LOCAL_PORT >= 1024 && API_LOCAL_PORT <= 65535 )) \
  || fail "API tunnel port must be between 1024 and 65535"
[[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required"
command -v hcloud >/dev/null || fail "hcloud CLI is required"
command -v jq >/dev/null || fail "jq is required"
[[ "$MAX_BACKUP_AGE_SECONDS" =~ ^[1-9][0-9]*$ ]] \
  || fail "--max-backup-age-seconds must be a positive integer"

# A normal project confirmation is insufficient while a resumable profile
# migration owns the cluster. This protects long-running backup/resize/restore
# campaigns from a stale cleanup command in another terminal or task. The
# second phrase is deliberately project-specific and is still required when a
# migration lock process has died, because durable state remains authoritative.
ACTIVE_MIGRATION_STATES=()
while IFS= read -r state_file; do
  [[ -s "$state_file" ]] || continue
  state_project=$(jq -r '.project // ""' "$state_file" 2>/dev/null || true)
  state_status=$(jq -r '.status // ""' "$state_file" 2>/dev/null || true)
  if [[ "$state_project" == "$PROJECT" && "$state_status" == in_progress ]]; then
    ACTIVE_MIGRATION_STATES+=("$state_file")
  fi
done < <(find "$ROOT_DIR" -maxdepth 7 -type f -name state.json \
  \( -path '*/.migration-state/*' -o -path '*/migration-proof/*' \) -print 2>/dev/null | sort)
if (( ${#ACTIVE_MIGRATION_STATES[@]} > 0 )); then
  expected_active_confirmation="DESTROY_ACTIVE_MIGRATION_${PROJECT}"
  [[ "$ACTIVE_MIGRATION_CONFIRM" == "$expected_active_confirmation" ]] \
    || fail "project $PROJECT has an in-progress profile migration (${ACTIVE_MIGRATION_STATES[0]}); rerun only after recovery or add --confirm-active-migration $expected_active_confirmation"
fi

sha256_file() {
  if command -v sha256sum >/dev/null; then
    sha256sum "$1" | awk '{print $1}'
  else
    shasum -a 256 "$1" | awk '{print $1}'
  fi
}

verify_required_backup_receipt() {
  local receipt="$1" archive checksum source_uid expected_sha actual_sha
  local endpoint bucket receipt_key archive_key checksum_key remote_sha sidecar_sha
  [[ -f "$receipt" && -r "$receipt" && -s "$receipt" ]] \
    || fail "required backup receipt is missing, unreadable, or empty: $receipt"
  [[ "$receipt" == *.manifest.json ]] \
    || fail "required backup receipt must end with .manifest.json"
  archive="${receipt%.manifest.json}"
  checksum="${archive}.sha256"
  [[ -f "$archive" && -r "$archive" && -s "$archive" ]] \
    || fail "required encrypted backup archive is missing: $archive"
  [[ -f "$checksum" && -r "$checksum" && -s "$checksum" ]] \
    || fail "required encrypted backup checksum is missing: $checksum"
  command -v kubectl >/dev/null || fail "kubectl is required for the backup receipt source-identity gate"
  source_uid=$(kubectl get namespace kube-system -o jsonpath='{.metadata.uid}' 2>/dev/null) \
    || fail "cannot read the source cluster UID for the backup receipt gate"
  [[ -n "$source_uid" ]] || fail "source cluster UID is empty"
  jq -e --arg project "$PROJECT" --arg sourceUid "$source_uid" \
    --arg archive "$(basename "$archive")" --argjson maxAge "$MAX_BACKUP_AGE_SECONDS" '
      .schema_version == 2 and
      .receipt_type == "encrypted-cluster-backup" and
      .project == $project and .source_cluster_uid == $sourceUid and
      .archive == $archive and .completeness == "complete" and
      (.velero_backup_name | type == "string" and length > 0) and
      (.velero_storage_prefix | type == "string" and length > 0) and
      (.sha256 | test("^[a-f0-9]{64}$")) and
      ((now - (.created_at | fromdateiso8601)) >= -300) and
      ((now - (.created_at | fromdateiso8601)) <= $maxAge) and
      .remote.published == true and
      .remote.download_sha256_verified == true and
      .remote.receipt_uploaded_last == true and
      .remote.publication_state == "complete" and
      (.remote.endpoint | type == "string" and length > 0) and
      (.remote.bucket | type == "string" and length > 0) and
      (.remote.archive_key | type == "string" and length > 0) and
      (.remote.checksum_key | type == "string" and length > 0) and
      (.remote.receipt_key | type == "string" and length > 0)
    ' "$receipt" >/dev/null \
    || fail "backup receipt is stale, incomplete, or does not match project/source cluster identity"
  expected_sha=$(jq -r '.sha256' "$receipt")
  actual_sha=$(sha256_file "$archive")
  sidecar_sha=$(awk 'NR == 1 {print $1}' "$checksum")
  [[ "$actual_sha" == "$expected_sha" && "$sidecar_sha" == "$expected_sha" ]] \
    || fail "local encrypted backup archive does not match its receipt and checksum"

  command -v aws >/dev/null || fail "aws CLI is required to re-verify the remote backup before teardown"
  command -v cmp >/dev/null || fail "cmp is required to verify the remote receipt"
  endpoint=$(jq -r '.remote.endpoint' "$receipt")
  bucket=$(jq -r '.remote.bucket' "$receipt")
  [[ -n "${BACKUP_DR_ENDPOINT:-}" && "$BACKUP_DR_ENDPOINT" == "$endpoint" ]] \
    || fail "BACKUP_DR_ENDPOINT must exactly match the verified receipt endpoint"
  [[ -n "${BACKUP_DR_BUCKET:-}" && "$BACKUP_DR_BUCKET" == "$bucket" ]] \
    || fail "BACKUP_DR_BUCKET must exactly match the verified receipt bucket"
  [[ -n "${BACKUP_DR_ACCESS_KEY:-}" && -n "${BACKUP_DR_SECRET_KEY:-}" ]] \
    || fail "BACKUP_DR_ACCESS_KEY and BACKUP_DR_SECRET_KEY are required for remote teardown verification"
  receipt_key=$(jq -r '.remote.receipt_key' "$receipt")
  archive_key=$(jq -r '.remote.archive_key' "$receipt")
  checksum_key=$(jq -r '.remote.checksum_key' "$receipt")
  TEARDOWN_GATE_DIR=$(mktemp -d "${TMPDIR:-/tmp}/teardown-backup-gate.XXXXXX")
  chmod 0700 "$TEARDOWN_GATE_DIR"
  AWS_ACCESS_KEY_ID="$BACKUP_DR_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$BACKUP_DR_SECRET_KEY" \
    AWS_DEFAULT_REGION="${BACKUP_DR_REGION:-us-east-1}" AWS_EC2_METADATA_DISABLED=true \
    aws --endpoint-url "$endpoint" s3 cp "s3://${bucket}/${receipt_key}" \
    "$TEARDOWN_GATE_DIR/receipt.json" --only-show-errors \
    || fail "remote backup completion receipt is unavailable"
  cmp -s "$receipt" "$TEARDOWN_GATE_DIR/receipt.json" \
    || fail "remote backup completion receipt differs from the local verified receipt"
  AWS_ACCESS_KEY_ID="$BACKUP_DR_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$BACKUP_DR_SECRET_KEY" \
    AWS_DEFAULT_REGION="${BACKUP_DR_REGION:-us-east-1}" AWS_EC2_METADATA_DISABLED=true \
    aws --endpoint-url "$endpoint" s3 cp "s3://${bucket}/${checksum_key}" \
    "$TEARDOWN_GATE_DIR/archive.sha256" --only-show-errors \
    || fail "remote backup checksum is unavailable"
  remote_sha=$(awk 'NR == 1 {print $1}' "$TEARDOWN_GATE_DIR/archive.sha256")
  [[ "$remote_sha" == "$expected_sha" ]] || fail "remote backup checksum differs from the receipt"
  AWS_ACCESS_KEY_ID="$BACKUP_DR_ACCESS_KEY" AWS_SECRET_ACCESS_KEY="$BACKUP_DR_SECRET_KEY" \
    AWS_DEFAULT_REGION="${BACKUP_DR_REGION:-us-east-1}" AWS_EC2_METADATA_DISABLED=true \
    aws --endpoint-url "$endpoint" s3 cp "s3://${bucket}/${archive_key}" \
    "$TEARDOWN_GATE_DIR/archive" --only-show-errors \
    || fail "remote encrypted backup archive is unavailable"
  [[ "$(sha256_file "$TEARDOWN_GATE_DIR/archive")" == "$expected_sha" ]] \
    || fail "remote encrypted backup archive failed the teardown SHA-256 gate"
  rm -rf "$TEARDOWN_GATE_DIR"
  TEARDOWN_GATE_DIR=""
  log "Verified recent local and remote recovery bundle for source cluster UID $source_uid"
}

if [[ -n "$BACKUP_RECEIPT" ]]; then
  verify_required_backup_receipt "$BACKUP_RECEIPT"
fi

FAILURES=0
PROJECT_VOLUME_IDS=()
PROJECT_VOLUME_COUNT=0

add_project_volume_id() {
  local candidate="$1" existing
  [[ "$candidate" =~ ^[0-9]+$ ]] || return 0
  for existing in "${PROJECT_VOLUME_IDS[@]:-}"; do
    [[ "$existing" != "$candidate" ]] || return 0
  done
  PROJECT_VOLUME_IDS+=("$candidate")
  PROJECT_VOLUME_COUNT=$((PROJECT_VOLUME_COUNT + 1))
}

list_project_labeled() {
  hcloud "$1" list -o json 2>/dev/null \
    | jq -r --arg project "$PROJECT" '.[] | select(.labels.project == $project) | .name'
}

delete_project_labeled() {
  local resource="$1" name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    log "Deleting ${resource}: ${name}"
    if ! hcloud "$resource" delete "$name"; then
      printf '  FAILED to delete %s: %s\n' "$resource" "$name" >&2
      FAILURES=$((FAILURES + 1))
    fi
  done < <(list_project_labeled "$resource")
}

printf '=== Tearing down Hetzner project resources: %s ===\n' "$PROJECT"

# CSI volumes use provider-generated pvc-* names, so name-prefix matching is
# insufficient. Capture the immutable IDs of every volume currently attached
# to an exactly project-labeled server before deleting those servers. Project
# names can overlap (for example, "medium" and "medium-optimized"), therefore
# neither servers nor volumes may be selected by a project-name prefix here.
# Unrelated detached volumes and volumes attached to non-project servers are
# never selected.
project_servers_json=$(hcloud server list -o json \
  | jq -c --arg project "$PROJECT" \
    '[.[] | select(.labels.project == $project) | {id, name}]')
project_server_ids=$(jq -c '[.[].id]' <<<"$project_servers_json")
project_server_names=$(jq -c '[.[].name]' <<<"$project_servers_json")
while IFS= read -r volume_id; do
  [[ -n "$volume_id" ]] || continue
  add_project_volume_id "$volume_id"
done < <(hcloud volume list -o json \
  | jq -r --arg project "$PROJECT" --argjson server_ids "$project_server_ids" \
    '.[] | select(.labels.project == $project or (.server != null and (.server as $server | $server_ids | index($server)))) | .id' \
  | sort -u)

# A retained PVC may already be detached while its PV still records the exact
# provider volumeHandle. Read those handles only when every node in the active
# context belongs to this project; this prevents an unrelated kubeconfig from
# broadening teardown scope.
if command -v kubectl >/dev/null 2>&1 && nodes_json=$(kubectl get nodes -o json 2>/dev/null); then
  matching_context=$(jq -r --argjson server_names "$project_server_names" \
    '(.items | length) > 0 and all(.items[]; .metadata.name as $name | $server_names | index($name) != null)' \
    <<<"$nodes_json")
  if [[ "$matching_context" == true ]]; then
    while IFS= read -r volume_id; do
      add_project_volume_id "$volume_id"
    done < <(kubectl get pv -o json 2>/dev/null \
      | jq -r '.items[] | select(.spec.csi.driver == "csi.hetzner.cloud") | .spec.csi.volumeHandle' \
      | sort -u)
  else
    log "Skipping PV volume capture because the active Kubernetes context is not exclusively this project"
  fi
fi
log "Captured ${PROJECT_VOLUME_COUNT} project volume ID(s) before server deletion"

# Remove ingress first, then compute. Volumes are handled only after servers
# have disappeared so attached CSI volumes are not silently skipped.
delete_project_labeled load-balancer
delete_project_labeled server

for _ in 1 2 3 4 5 6; do
  [[ -z "$(list_project_labeled server)" ]] && break
  sleep 5
done

for volume_id in "${PROJECT_VOLUME_IDS[@]:-}"; do
  [[ -n "$volume_id" ]] || continue
  hcloud volume describe "$volume_id" >/dev/null 2>&1 || continue
  log "Detaching captured project volume if attached: $volume_id"
  hcloud volume detach "$volume_id" >/dev/null 2>&1 || true
  volume_detached=false
  for ((attempt = 0; attempt < 60; attempt++)); do
    if ! volume_json=$(hcloud volume describe "$volume_id" -o json 2>/dev/null); then
      volume_detached=true
      break
    fi
    if [[ "$(jq -r '.server == null' <<<"$volume_json")" == true ]]; then
      volume_detached=true
      break
    fi
    sleep 2
  done
  if [[ "$volume_detached" != true ]]; then
    printf '  FAILED waiting for captured project volume to detach: %s\n' "$volume_id" >&2
    FAILURES=$((FAILURES + 1))
    continue
  fi
  hcloud volume describe "$volume_id" >/dev/null 2>&1 || continue
  log "Deleting captured project volume: $volume_id"
  if ! hcloud volume delete "$volume_id"; then
    printf '  FAILED to delete captured project volume: %s\n' "$volume_id" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

delete_project_labeled firewall
delete_project_labeled placement-group

# Placement groups created by releases before project labels were added are
# still safe to remove by their single exact conventional name. Never use a
# project-name prefix here: projects such as "medium" and
# "medium-optimized" may be torn down concurrently.
if hcloud placement-group describe "${PROJECT}-spread" >/dev/null 2>&1; then
  log "Deleting legacy exact placement-group: ${PROJECT}-spread"
  hcloud placement-group delete "${PROJECT}-spread" || FAILURES=$((FAILURES + 1))
fi

if hcloud ssh-key describe "${PROJECT}-key" >/dev/null 2>&1; then
  log "Deleting ssh-key: ${PROJECT}-key"
  hcloud ssh-key delete "${PROJECT}-key" || FAILURES=$((FAILURES + 1))
fi

if hcloud network describe "${PROJECT}-network" >/dev/null 2>&1; then
  while IFS= read -r subnet; do
    [[ -n "$subnet" ]] || continue
    log "Removing subnet: $subnet"
    hcloud network remove-subnet "${PROJECT}-network" --ip-range "$subnet" || FAILURES=$((FAILURES + 1))
  done < <(hcloud network describe "${PROJECT}-network" -o json | jq -r '.subnets[].ip_range')
  log "Deleting network: ${PROJECT}-network"
  hcloud network delete "${PROJECT}-network" || FAILURES=$((FAILURES + 1))
fi

REMAINING=""
for resource in load-balancer server volume firewall placement-group; do
  names="$(list_project_labeled "$resource")"
  [[ -z "$names" ]] || REMAINING+="${resource}: ${names}"$'\n'
done
for volume_id in "${PROJECT_VOLUME_IDS[@]:-}"; do
  [[ -n "$volume_id" ]] || continue
  if hcloud volume describe "$volume_id" >/dev/null 2>&1; then
    REMAINING+="captured volume: ${volume_id}"$'\n'
  fi
done
hcloud network describe "${PROJECT}-network" >/dev/null 2>&1 && REMAINING+="network: ${PROJECT}-network"$'\n'

# Stop only the API tunnel explicitly owned by this project. Never kill a
# process merely because it happens to use the same local port.
TUNNEL_PID_FILE="${HOME}/.kube/${PROJECT}-api-tunnel.pid"
if [[ -f "$TUNNEL_PID_FILE" ]]; then
  tunnel_pid=$(cat "$TUNNEL_PID_FILE")
  if [[ "$tunnel_pid" =~ ^[0-9]+$ ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
    tunnel_command=$(ps -p "$tunnel_pid" -o command=)
    if [[ "$tunnel_command" != *"kube-api-tunnel-supervisor.sh"* || "$tunnel_command" != *"--local-port ${API_LOCAL_PORT}"* ]]; then
      printf '  REFUSED to kill unmanaged PID %s: %s\n' "$tunnel_pid" "$tunnel_command" >&2
      FAILURES=$((FAILURES + 1))
      tunnel_pid=""
    fi
  fi
  if [[ -n "$tunnel_pid" && "$tunnel_pid" =~ ^[0-9]+$ ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
    log "Stopping managed Kubernetes API tunnel: $tunnel_pid"
    if kill "$tunnel_pid"; then
      # The supervisor may be inside its 15-second health-check sleep. Bash
      # defers the TERM trap until that foreground sleep returns, so allow one
      # complete interval plus scheduling headroom before declaring failure.
      for ((attempt = 0; attempt < 100; attempt++)); do
        kill -0 "$tunnel_pid" 2>/dev/null || break
        sleep 0.2
      done
      kill -0 "$tunnel_pid" 2>/dev/null && FAILURES=$((FAILURES + 1))
    else
      FAILURES=$((FAILURES + 1))
    fi
  fi
  rm -f "$TUNNEL_PID_FILE"
fi

if [[ "$FAILURES" -ne 0 || -n "$REMAINING" ]]; then
  printf 'Teardown verification FAILED (%d command failures).\n%s' "$FAILURES" "$REMAINING" >&2
  exit 1
fi

rm -f "${ROOT_DIR}/playbooks/${PROJECT}-infra-facts.yml"
rm -f "${HOME}/.ssh/known_hosts-${PROJECT}"
rm -rf "${HOME}/.ansible/cp/${PROJECT}" "${HOME}/.cache/ansible-k8s/${PROJECT}"
printf '=== Teardown verified complete: %s ===\n' "$PROJECT"
printf 'DNS zone and records were intentionally preserved.\n'
