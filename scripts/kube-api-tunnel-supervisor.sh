#!/usr/bin/env bash
set -euo pipefail

BASTION=""
TARGETS=()
LOCAL_PORT=16443
CHECK_INTERVAL=15
KUBECONFIG_FILE=""
KNOWN_HOSTS_FILE="${HOME}/.ssh/known_hosts"
BACKEND_CONNECT_TIMEOUT=60
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROXY_SCRIPT="${SCRIPT_DIR}/kube-api-retry-proxy.py"

while (( $# > 0 )); do
  case "$1" in
    --bastion) BASTION="$2"; shift 2 ;;
    --target) TARGETS+=("$2"); shift 2 ;;
    --local-port) LOCAL_PORT="$2"; shift 2 ;;
    --check-interval) CHECK_INTERVAL="$2"; shift 2 ;;
    --kubeconfig) KUBECONFIG_FILE="$2"; shift 2 ;;
    --known-hosts-file) KNOWN_HOSTS_FILE="$2"; shift 2 ;;
    --backend-connect-timeout) BACKEND_CONNECT_TIMEOUT="$2"; shift 2 ;;
    *) printf 'Unknown argument: %s\n' "$1" >&2; exit 2 ;;
  esac
done

[[ -n "$BASTION" && ${#TARGETS[@]} -gt 0 ]] || {
  printf 'Usage: %s --bastion HOST --target HOST [--target HOST ...] [--local-port PORT] [--known-hosts-file FILE]\n' "$0" >&2
  exit 2
}
[[ "$LOCAL_PORT" =~ ^[0-9]+$ && "$CHECK_INTERVAL" =~ ^[0-9]+$ \
  && "$BACKEND_CONNECT_TIMEOUT" =~ ^[0-9]+$ ]] || {
  printf 'Port, check interval, and backend timeout must be positive integers\n' >&2
  exit 2
}
(( 10#$LOCAL_PORT >= 1024 && 10#$LOCAL_PORT <= 65535 \
  && 10#$CHECK_INTERVAL > 0 && 10#$BACKEND_CONNECT_TIMEOUT > 0 )) || {
  printf 'Port must be 1024-65535; intervals and timeouts must be positive\n' >&2
  exit 2
}
[[ -f "$KNOWN_HOSTS_FILE" ]] || {
  printf 'Known-hosts file does not exist: %s\n' "$KNOWN_HOSTS_FILE" >&2
  exit 2
}
[[ -f "$PROXY_SCRIPT" ]] || {
  printf 'Kubernetes API retry proxy does not exist: %s\n' "$PROXY_SCRIPT" >&2
  exit 2
}
command -v python3 >/dev/null 2>&1 || {
  printf 'python3 is required for the Kubernetes API retry proxy\n' >&2
  exit 2
}

child_pid=""
proxy_pid=""
runtime_dir=""

stop_ssh() {
  [[ -n "$child_pid" ]] || return 0
  if kill -0 "$child_pid" 2>/dev/null; then
    kill "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  child_pid=""
}
stop_child() {
  stop_ssh
  if [[ -n "$proxy_pid" ]] && kill -0 "$proxy_pid" 2>/dev/null; then
    kill "$proxy_pid" 2>/dev/null || true
    wait "$proxy_pid" 2>/dev/null || true
  fi
  proxy_pid=""
  [[ -z "$runtime_dir" ]] || rm -rf -- "$runtime_dir"
}
cleanup_exit() {
  status=$?
  trap - EXIT
  stop_child
  exit "$status"
}
trap 'stop_child; exit 0' INT TERM
trap cleanup_exit EXIT

runtime_dir="$(mktemp -d "/tmp/k8s-api-${UID}-${LOCAL_PORT}.XXXXXX")"
chmod 0700 "$runtime_dir"
backend_socket="${runtime_dir}/backend.sock"
proxy_ready_file="${runtime_dir}/proxy.ready"
python3 "$PROXY_SCRIPT" \
  --listen-host 127.0.0.1 \
  --listen-port "$LOCAL_PORT" \
  --backend-unix "$backend_socket" \
  --connect-timeout "$BACKEND_CONNECT_TIMEOUT" \
  --ready-file "$proxy_ready_file" &
proxy_pid=$!
for _ in $(seq 1 100); do
  [[ -f "$proxy_ready_file" ]] && break
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    wait "$proxy_pid" 2>/dev/null || true
    printf 'Kubernetes API retry proxy exited before becoming ready\n' >&2
    exit 1
  fi
  sleep 0.1
done
[[ -f "$proxy_ready_file" ]] || {
  printf 'Kubernetes API retry proxy did not bind port %s within 10 seconds\n' "$LOCAL_PORT" >&2
  exit 1
}

target_index=0
while true; do
  if ! kill -0 "$proxy_pid" 2>/dev/null; then
    printf 'Kubernetes API retry proxy exited unexpectedly\n' >&2
    exit 1
  fi
  target="${TARGETS[$target_index]}"
  rm -f -- "$backend_socket"
  ssh -o BatchMode=yes -o ExitOnForwardFailure=yes \
    -o ConnectTimeout=30 -o ConnectionAttempts=3 \
    -o IPQoS=none -o TCPKeepAlive=yes \
    -o ServerAliveInterval=15 -o ServerAliveCountMax=3 \
    -o StreamLocalBindUnlink=yes \
    -o StrictHostKeyChecking=accept-new \
    -o "UserKnownHostsFile=${KNOWN_HOSTS_FILE}" -N \
    -L "${backend_socket}:${target}:6443" "root@${BASTION}" &
  child_pid=$!

  failures=0
  while kill -0 "$child_pid" 2>/dev/null; do
    if ! kill -0 "$proxy_pid" 2>/dev/null; then
      printf 'Kubernetes API retry proxy exited unexpectedly\n' >&2
      stop_ssh
      exit 1
    fi
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
        stop_ssh
        break
      fi
    fi
    sleep "$CHECK_INTERVAL"
  done

  [[ -z "$child_pid" ]] || wait "$child_pid" 2>/dev/null || true
  child_pid=""
  target_index=$(((target_index + 1) % ${#TARGETS[@]}))
  # Move directly to the next endpoint. A deliberate delay leaves the local
  # API port unbound and makes otherwise retryable Helm discovery calls fail
  # with connection refused during failover.
done
