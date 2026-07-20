#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${ROOT_DIR}/scripts/load-project-env.sh"

PROJECT="${1:-}"
CONFIRM=""
[[ "${2:-}" == "--confirm" ]] && CONFIRM="${3:-}"

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '  %s\n' "$*"; }

[[ -n "$PROJECT" ]] || fail "usage: $0 <project> --confirm <project>"
[[ "$PROJECT" =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]*$ ]] || fail "invalid project name"
[[ "$CONFIRM" == "$PROJECT" ]] || fail "confirmation must exactly match project '$PROJECT'"
[[ -n "${HCLOUD_TOKEN:-}" ]] || fail "HCLOUD_TOKEN is required"
command -v hcloud >/dev/null || fail "hcloud CLI is required"

PREFIX="${PROJECT}-"
FAILURES=0
PROJECT_VOLUME_IDS=()

add_project_volume_id() {
  local candidate="$1" existing
  [[ "$candidate" =~ ^[0-9]+$ ]] || return 0
  for existing in "${PROJECT_VOLUME_IDS[@]}"; do
    [[ "$existing" != "$candidate" ]] || return 0
  done
  PROJECT_VOLUME_IDS+=("$candidate")
}

list_prefixed() {
  hcloud "$1" list -o noheader -o columns=name 2>/dev/null | awk -v p="$PREFIX" 'index($0,p)==1'
}

delete_prefixed() {
  local resource="$1" name
  while IFS= read -r name; do
    [[ -n "$name" ]] || continue
    log "Deleting ${resource}: ${name}"
    if ! hcloud "$resource" delete "$name"; then
      printf '  FAILED to delete %s: %s\n' "$resource" "$name" >&2
      FAILURES=$((FAILURES + 1))
    fi
  done < <(list_prefixed "$resource")
}

printf '=== Tearing down Hetzner project resources: %s ===\n' "$PROJECT"

# CSI volumes use provider-generated pvc-* names, so name-prefix matching is
# insufficient. Capture the immutable IDs of every volume currently attached
# to a project server before deleting those servers. Unrelated detached
# volumes and volumes attached to non-project servers are never selected.
project_server_ids=$(hcloud server list -o json \
  | jq -c --arg prefix "$PREFIX" '[.[] | select(.name | startswith($prefix)) | .id]')
while IFS= read -r volume_id; do
  [[ -n "$volume_id" ]] || continue
  add_project_volume_id "$volume_id"
done < <(hcloud volume list -o json \
  | jq -r --argjson server_ids "$project_server_ids" --arg prefix "$PREFIX" \
    '.[] | select((.server != null and (.server as $server | $server_ids | index($server))) or (.name | startswith($prefix))) | .id' \
  | sort -u)

# A retained PVC may already be detached while its PV still records the exact
# provider volumeHandle. Read those handles only when every node in the active
# context belongs to this project; this prevents an unrelated kubeconfig from
# broadening teardown scope.
if command -v kubectl >/dev/null 2>&1 && nodes_json=$(kubectl get nodes -o json 2>/dev/null); then
  matching_context=$(jq -r --arg prefix "$PREFIX" \
    '(.items | length) > 0 and all(.items[]; .metadata.name | startswith($prefix))' \
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
log "Captured ${#PROJECT_VOLUME_IDS[@]} project volume ID(s) before server deletion"

# Remove ingress first, then compute. Volumes are handled only after servers
# have disappeared so attached CSI volumes are not silently skipped.
delete_prefixed load-balancer
delete_prefixed server

for _ in 1 2 3 4 5 6; do
  [[ -z "$(list_prefixed server)" ]] && break
  sleep 5
done

for volume_id in "${PROJECT_VOLUME_IDS[@]}"; do
  hcloud volume describe "$volume_id" >/dev/null 2>&1 || continue
  log "Detaching captured project volume if attached: $volume_id"
  hcloud volume detach "$volume_id" >/dev/null 2>&1 || true
  log "Deleting captured project volume: $volume_id"
  if ! hcloud volume delete "$volume_id"; then
    printf '  FAILED to delete captured project volume: %s\n' "$volume_id" >&2
    FAILURES=$((FAILURES + 1))
  fi
done

delete_prefixed firewall
delete_prefixed placement-group

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
  names="$(list_prefixed "$resource")"
  [[ -z "$names" ]] || REMAINING+="${resource}: ${names}"$'\n'
done
for volume_id in "${PROJECT_VOLUME_IDS[@]}"; do
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
    if [[ "$tunnel_command" != *"kube-api-tunnel-supervisor.sh"* || "$tunnel_command" != *"--local-port 16443"* ]]; then
      printf '  REFUSED to kill unmanaged PID %s: %s\n' "$tunnel_pid" "$tunnel_command" >&2
      FAILURES=$((FAILURES + 1))
      tunnel_pid=""
    fi
  fi
  if [[ -n "$tunnel_pid" && "$tunnel_pid" =~ ^[0-9]+$ ]] && kill -0 "$tunnel_pid" 2>/dev/null; then
    log "Stopping managed Kubernetes API tunnel: $tunnel_pid"
    if kill "$tunnel_pid"; then
      for _ in 1 2 3 4 5 6 7 8 9 10; do
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
printf '=== Teardown verified complete: %s ===\n' "$PROJECT"
printf 'DNS zone and records were intentionally preserved.\n'
