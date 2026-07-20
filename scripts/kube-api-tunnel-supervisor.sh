#!/usr/bin/env bash
set -euo pipefail

BASTION=""
TARGETS=()
LOCAL_PORT=16443
CHECK_INTERVAL=15
KUBECONFIG_FILE=""

while (( $# > 0 )); do
  case "$1" in
    --bastion) BASTION="$2"; shift 2 ;;
    --target) TARGETS+=("$2"); shift 2 ;;
    --local-port) LOCAL_PORT="$2"; shift 2 ;;
    --check-interval) CHECK_INTERVAL="$2"; shift 2 ;;
    --kubeconfig) KUBECONFIG_FILE="$2"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -n "$BASTION" && ${#TARGETS[@]} -gt 0 ]] || {
  printf 'Usage: %s --bastion HOST --target HOST [--target HOST ...] [--local-port PORT]\n' "$0" >&2
  exit 2
}
[[ "$LOCAL_PORT" =~ ^[0-9]+$ && "$CHECK_INTERVAL" =~ ^[0-9]+$ ]] || {
  printf 'Port and check interval must be positive integers\n' >&2
  exit 2
}

child_pid=""
stop_child() {
  [[ -n "$child_pid" ]] || return 0
  if kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
}
trap 'stop_child; exit 0' INT TERM EXIT

target_index=0
while true; do
  target="${TARGETS[$target_index]}"
  ssh -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ConnectTimeout=30 -o ConnectionAttempts=3 \
    -o IPQoS=none -o TCPKeepAlive=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o StrictHostKeyChecking=accept-new -N \
    -L "127.0.0.1:${LOCAL_PORT}:${target}:6443" "root@${BASTION}" &
  child_pid=$!

  failures=0
  while kill -0 "$child_pid" 2>/dev/null; do
    sleep "$CHECK_INTERVAL"
    healthy=1
    if [[ -n "$KUBECONFIG_FILE" ]]; then
      if KUBECONFIG="$KUBECONFIG_FILE" kubectl \
        --server="https://127.0.0.1:${LOCAL_PORT}" --request-timeout=8s \
        get --raw=/readyz >/dev/null 2>&1; then
        healthy=0
      fi
    elif curl -ksS --connect-timeout 3 --max-time 8 \
      -o /dev/null "https://127.0.0.1:${LOCAL_PORT}/readyz"; then
      healthy=0
    fi
    if (( healthy == 0 )); then
      failures=0
    else
      failures=$((failures + 1))
      if (( failures >= 2 )); then
        stop_child
        break
      fi
    fi
  done

  [[ -z "$child_pid" ]] || wait "$child_pid" 2>/dev/null || true
  child_pid=""
  target_index=$(((target_index + 1) % ${#TARGETS[@]}))
  # Move directly to the next endpoint. A deliberate delay leaves the local
  # API port unbound and makes otherwise retryable Helm discovery calls fail
  # with connection refused during failover.
done
