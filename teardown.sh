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

# Remove ingress first, then compute. Volumes are handled only after servers
# have disappeared so attached CSI volumes are not silently skipped.
delete_prefixed load-balancer
delete_prefixed server

for _ in 1 2 3 4 5 6; do
  [[ -z "$(list_prefixed server)" ]] && break
  sleep 5
done

while IFS= read -r volume; do
  [[ -n "$volume" ]] || continue
  log "Detaching volume if attached: $volume"
  hcloud volume detach "$volume" >/dev/null 2>&1 || true
  log "Deleting volume: $volume"
  if ! hcloud volume delete "$volume"; then
    printf '  FAILED to delete volume: %s\n' "$volume" >&2
    FAILURES=$((FAILURES + 1))
  fi
done < <(list_prefixed volume)

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
hcloud network describe "${PROJECT}-network" >/dev/null 2>&1 && REMAINING+="network: ${PROJECT}-network"$'\n'

if [[ "$FAILURES" -ne 0 || -n "$REMAINING" ]]; then
  printf 'Teardown verification FAILED (%d command failures).\n%s' "$FAILURES" "$REMAINING" >&2
  exit 1
fi

rm -f "${ROOT_DIR}/playbooks/${PROJECT}-infra-facts.yml"
printf '=== Teardown verified complete: %s ===\n' "$PROJECT"
printf 'DNS zone and records were intentionally preserved.\n'
