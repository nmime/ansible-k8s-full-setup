#!/usr/bin/env bash
# Profile-aware, mutating smoke tests for an explicitly authorized disposable cluster.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
if [[ -x "$ROOT_DIR/.venv/bin/ansible-vault" ]]; then
  PATH="$ROOT_DIR/.venv/bin:$PATH"
  export PATH
fi
ENV_LOADER="$SCRIPT_DIR/load-project-env.sh"
# shellcheck source=scripts/load-project-env.sh
source "$ENV_LOADER"
CONFIG_FILE="$ROOT_DIR/platform-orchestrator/platform.yaml"
VAULT_INIT_FILE=""
DRY_RUN=false
RUN_ID="${LIVE_SMOKE_RUN_ID:-$(date +%s)}"

usage() {
  cat <<'EOF'
Usage: live-tier-smoke.sh [--config FILE] [--vault-init FILE] [--dry-run]

Runs profile-aware namespace, S3, PostgreSQL, Vault, metrics, logs, GitOps,
and public TLS data-path checks. Test objects are uniquely named and removed.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="${2:?missing config path}"; shift 2 ;;
    --vault-init) VAULT_INIT_FILE="${2:?missing Vault init path}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

run_id_pattern='^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$'
[[ "$RUN_ID" =~ $run_id_pattern ]] || {
  echo "LIVE_SMOKE_RUN_ID must be a lowercase DNS label (maximum 32 characters)" >&2
  exit 2
}
[[ -f "$CONFIG_FILE" ]] || { echo "Config not found: $CONFIG_FILE" >&2; exit 2; }
command -v yq >/dev/null || { echo "yq is required" >&2; exit 2; }
if [[ -z "$VAULT_INIT_FILE" ]]; then
  project=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
  VAULT_INIT_FILE="$ROOT_DIR/playbooks/.vault-init-${project}.json"
fi

enabled() { [[ "$(yq -r "$1 // false" "$CONFIG_FILE")" == true ]]; }
log() { printf '[live-smoke] %s\n' "$*"; }
run() {
  if $DRY_RUN; then printf '[dry-run]'; printf ' %q' "$@"; printf '\n'; else "$@"; fi
}

check_namespace() {
  local path="$1" namespace="$2"
  if enabled "$path"; then
    log "require namespace $namespace ($path)"
    run kubectl get namespace "$namespace" -o name
  else
    log "require no workloads in $namespace ($path disabled)"
    if ! $DRY_RUN && kubectl get namespace "$namespace" >/dev/null 2>&1; then
      local workloads releases
      workloads=$(kubectl get deployments,statefulsets,daemonsets,cronjobs -n "$namespace" -o name 2>/dev/null | wc -l | tr -d ' ')
      releases=$(helm list -n "$namespace" --short 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')
      if [[ "$workloads" -ne 0 || "$releases" -ne 0 ]]; then
        echo "Disabled component still has resources in $namespace: workloads=$workloads helm-releases=$releases" >&2
        return 1
      fi
    fi
  fi
}

curl_from_cluster() {
  local name="$1" url="$2"
  run kubectl run "live-smoke-${name}-${RUN_ID}" -n default --rm -i --restart=Never \
    --image=curlimages/curl:8.17.0 --command -- curl -kfsS --retry 6 --retry-delay 5 --max-time 30 "$url"
}

smoke_s3() {
  local pod="live-smoke-s3-${RUN_ID}"
  log "SeaweedFS S3 write/read/delete"
  if $DRY_RUN; then echo "[dry-run] create and remove $pod"; return; fi
  trap 'kubectl delete pod -n storage "'"$pod"'" --ignore-not-found --wait=false >/dev/null 2>&1 || true' EXIT
  kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: storage
spec:
  restartPolicy: Never
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    runAsGroup: 1000
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: aws-cli
      image: amazon/aws-cli:2.34.48
      command: ["/bin/sh", "-ec"]
      args:
        - |
          key="s3://backups/live-smoke/$RUN_ID"
          cleanup() { aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 rm "\$key" >/dev/null 2>&1 || true; }
          trap cleanup EXIT
          value="live-smoke-$RUN_ID"
          printf '%s' "\$value" >/tmp/value
          aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 cp /tmp/value "\$key"
          aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 cp "\$key" /tmp/read
          test "\$(cat /tmp/read)" = "\$value"
          cleanup
          test -z "\$(aws --endpoint-url http://seaweedfs-filer.storage.svc.cluster.local:8333 s3 ls "\$key")"
          trap - EXIT
      envFrom:
        - secretRef:
            name: seaweedfs-s3-config
      env:
        - name: AWS_DEFAULT_REGION
          value: us-east-1
        - name: AWS_EC2_METADATA_DISABLED
          value: "true"
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: false
EOF
  kubectl wait -n storage --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=5m
  kubectl logs -n storage "$pod"
  kubectl delete pod -n storage "$pod" --wait=true
  trap - EXIT
}

smoke_postgresql() {
  local pod
  if $DRY_RUN; then echo "[dry-run] PostgreSQL transaction write/read/rollback"; return; fi
  pod=$(kubectl get pods -n databases -l postgres-operator.crunchydata.com/role=primary \
    -o jsonpath='{.items[0].metadata.name}')
  [[ -n "$pod" ]] || { echo "PostgreSQL primary pod not found" >&2; return 1; }
  log "PostgreSQL transaction write/read/rollback on $pod"
  run kubectl exec -n databases "$pod" -c database -- psql -U postgres -d postgres \
    -v ON_ERROR_STOP=1 -Atc "BEGIN; CREATE TEMP TABLE live_smoke(v text); INSERT INTO live_smoke VALUES ('${RUN_ID}'); SELECT v FROM live_smoke; ROLLBACK;"
}

vault_exec() {
  local token="$1"
  shift
  printf '%s\n' "$token" | kubectl exec -i -n vault vault-0 -- \
    sh -ec 'IFS= read -r VAULT_TOKEN; export VAULT_TOKEN; exec vault "$@"' sh "$@"
}

smoke_vault() {
  local token value="live-smoke-${RUN_ID}" actual output rc=0
  if $DRY_RUN; then echo "[dry-run] Vault KV write/read/delete"; return; fi
  [[ -f "$VAULT_INIT_FILE" ]] || { echo "Encrypted Vault init material not found: $VAULT_INIT_FILE" >&2; return 1; }
  [[ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ]] || { echo "ANSIBLE_VAULT_PASSWORD_FILE is required" >&2; return 1; }
  token=$(ansible-vault view --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE" "$VAULT_INIT_FILE" | jq -r .root_token)
  [[ -n "$token" && "$token" != null ]] || { echo "Vault root token is unavailable" >&2; return 1; }
  log "Vault KV write/read/delete"
  if ! vault_exec "$token" kv put "secret/live-smoke/${RUN_ID}" value="$value" >/dev/null; then
    rc=1
  fi
  actual=$(vault_exec "$token" kv get -field=value \
    "secret/live-smoke/${RUN_ID}" 2>/dev/null || true)
  [[ "$actual" == "$value" ]] || { echo "Vault read-after-write mismatch" >&2; rc=1; }
  if ! vault_exec "$token" kv metadata delete \
    "secret/live-smoke/${RUN_ID}" >/dev/null; then
    rc=1
  fi
  output=$(vault_exec "$token" kv metadata get \
    "secret/live-smoke/${RUN_ID}" 2>&1) && rc=1
  grep -q 'No value found' <<<"$output" || rc=1
  unset token
  return "$rc"
}

smoke_keda_aggregated_api() {
  local available kind
  log "KEDA aggregated external-metrics API discovery"
  if $DRY_RUN; then echo "[dry-run] verify external.metrics.k8s.io APIService"; return; fi
  available=$(kubectl get apiservice v1beta1.external.metrics.k8s.io \
    -o jsonpath='{.status.conditions[?(@.type=="Available")].status}')
  [[ "$available" == True ]] || {
    kubectl get apiservice v1beta1.external.metrics.k8s.io -o yaml >&2
    echo "KEDA external metrics APIService is unavailable" >&2
    return 1
  }
  kind=$(kubectl get --raw /apis/external.metrics.k8s.io/v1beta1 | jq -r .kind)
  [[ "$kind" == APIResourceList ]] || {
    echo "Unexpected KEDA external metrics discovery response: $kind" >&2
    return 1
  }
}

smoke_logs() {
  local stack pod health
  stack=$(yq -r '.observability.logging.stack // "loki"' "$CONFIG_FILE")
  case "$stack" in
    loki)
      curl_from_cluster logs 'http://loki-gateway.monitoring.svc/loki/api/v1/status/buildinfo'
      ;;
    elk)
      if $DRY_RUN; then
        echo "[dry-run] verify authenticated Elasticsearch cluster health"
        return
      fi
      pod=$(kubectl get pods -n elasticsearch -l app=elasticsearch,role=data \
        -o jsonpath='{.items[0].metadata.name}')
      [[ -n "$pod" ]] || { echo "Elasticsearch data pod not found" >&2; return 1; }
      log "authenticated Elasticsearch cluster health on $pod"
      # shellcheck disable=SC2016 # password expands inside the pod shell.
      health=$(kubectl exec -n elasticsearch "$pod" -- bash -ec \
        'curl -fsSk -u "elastic:$ELASTIC_PASSWORD" https://localhost:9200/_cluster/health')
      jq -e '(.status == "green" or .status == "yellow") and .number_of_nodes >= 1' \
        <<<"$health" >/dev/null
      ;;
    *)
      echo "Unsupported observability logging stack: $stack" >&2
      return 1
      ;;
  esac
}

smoke_gateway_routes() {
  local routes route_ns gateway_ns gateway_name listener host path address port pod
  routes=$(kubectl get httproutes -A -o json | jq -r '
    .items[] as $route
    | $route.spec.parentRefs[]?
    | select((.kind // "Gateway") == "Gateway")
    | ($route.spec.rules[0].matches[0].path.value // "/") as $path
    | [$route.metadata.namespace, (.namespace // $route.metadata.namespace), .name,
       (.sectionName // ""), $route.spec.hostnames[]?, $path]
    | @tsv')
  [[ -n "$routes" ]] || { echo "No Gateway API HTTPRoute hostnames found" >&2; return 1; }
  while IFS=$'\t' read -r route_ns gateway_ns gateway_name listener host path; do
    [[ -n "$host" ]] || continue
    address=$(kubectl get gateway "$gateway_name" -n "$gateway_ns" -o jsonpath='{.status.addresses[0].value}')
    port=$(kubectl get gateway "$gateway_name" -n "$gateway_ns" -o json \
      | jq -r --arg listener "$listener" '.spec.listeners[] | select($listener == "" or .name == $listener) | .port' \
      | head -1)
    [[ -n "$address" && -n "$port" ]] || {
      echo "Gateway address/port unavailable: $gateway_ns/$gateway_name listener=$listener" >&2
      return 1
    }
    pod="live-smoke-route-${RUN_ID}-$(printf '%s' "$host" | cksum | awk '{print $1}')"
    log "Gateway TLS route $route_ns/$host via $gateway_ns/$gateway_name:$port"
    # The single-quoted program is intentionally evaluated inside the probe pod.
    # shellcheck disable=SC2016
    kubectl run "$pod" -n default --rm --attach=true --restart=Never \
      --image=curlimages/curl:8.17.0 --command -- sh -ec '
        status=$(curl -sS --proto "=https" --tlsv1.2 -o /dev/null -w "%{http_code}" \
          --retry 4 --retry-all-errors --retry-delay 3 --max-time 30 \
          --resolve "$1:$3:$2" "https://$1:$3$4")
        # 426 is a valid reachability proof for WebSocket-only endpoints such
        # as GitLab KAS: plain HTTPS reached the backend, which correctly asks
        # the client to upgrade the protocol.
        case "$status" in 2??|3??|400|401|403|405|426) printf "%s%s -> %s\n" "$1" "$4" "$status" ;; *) exit 1 ;; esac
      ' sh "$host" "$address" "$port" "$path"
  done <<<"$routes"
}

log "profile-aware namespace contract"
check_namespace '.storage.enabled' storage
check_namespace '.secrets.enabled' vault
check_namespace '.databases.enabled' databases
check_namespace '.gitops.enabled' argocd
check_namespace '.observability.enabled' monitoring
check_namespace '.autoscaling.enabled' keda
check_namespace '.gitlab.enabled' gitlab
check_namespace '.dragonfly.enabled' dragonfly
check_namespace '.temporal.enabled' temporal
check_namespace '.coroot.enabled' coroot
check_namespace '.postal.enabled' postal
check_namespace '.backup.enabled' velero
check_namespace '.glitchtip.enabled' glitchtip

enabled '.storage.enabled' && smoke_s3
enabled '.databases.enabled' && enabled '.databases.postgresql.enabled' && smoke_postgresql
enabled '.secrets.enabled' && smoke_vault
enabled '.autoscaling.enabled' && smoke_keda_aggregated_api
if enabled '.observability.metrics.enabled'; then
  tier=$(yq -r '.tier' "$CONFIG_FILE")
  if [[ "$tier" == minimal || "$tier" == small ]]; then
    curl_from_cluster metrics 'http://vmsingle-vmsingle.monitoring.svc:8429/health'
  else
    curl_from_cluster metrics 'http://vmselect-vmcluster.monitoring.svc:8481/health'
  fi
fi
enabled '.observability.logging.enabled' && smoke_logs
enabled '.observability.grafana.enabled' && curl_from_cluster grafana 'http://grafana.monitoring.svc/api/health'
enabled '.gitops.enabled' && curl_from_cluster argocd 'https://argocd-server.argocd.svc/healthz'
$DRY_RUN || smoke_gateway_routes

log "all profile-aware data-path tests passed"
