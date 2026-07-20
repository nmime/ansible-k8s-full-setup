#!/usr/bin/env bash
# Bounded, profile-aware live load test for an explicitly disposable cluster.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
ENV_LOADER="$SCRIPT_DIR/load-project-env.sh"
# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "$ENV_LOADER"

CONFIG_FILE="$ROOT_DIR/platform-orchestrator/platform.yaml"
KUBECONFIG_FILE="${KUBECONFIG:-}"
VAULT_INIT_FILE=""
OUTPUT_DIR=""
HTTP_URL=""
RUN_ID="${TIER_LOAD_RUN_ID:-$(date -u +%y%m%d-%H%M%S)}"
DRY_RUN=false
CLIENTS=""
HTTP_REQUESTS=""
S3_OBJECTS=""
PG_TRANSACTIONS=""
VAULT_OPERATIONS=""
DRAGONFLY_REQUESTS=""
PHASE_TIMEOUT=900
MAX_ERROR_PERCENT="1.0"
MAX_RESTART_DELTA=10

# Version-pinned load clients. Never replace these with floating tags.
HTTP_IMAGE="curlimages/curl:8.17.0"
S3_IMAGE="amazon/aws-cli:2.34.48"
POSTGRES_IMAGE="postgres:18.2-alpine3.23"
VAULT_IMAGE="hashicorp/vault:2.0.3"
DRAGONFLY_IMAGE="redis:7.4.7-alpine3.21"

usage() {
  cat <<'EOF'
Usage: tier-load-test.sh [OPTIONS]

Profile-aware phases: HTTP, SeaweedFS S3, PostgreSQL, Vault, and Dragonfly.
Disabled technologies are recorded as skipped. Each mutating phase owns a
unique prefix and is cleaned before the next health gate.

Options:
  --config FILE                 Profile YAML
  --kubeconfig FILE             Explicit kubeconfig
  --vault-init FILE             Encrypted Vault init JSON
  --output DIR                  Evidence root
  --run-id ID                   Unique alphanumeric/hyphen run ID
  --http-url URL                Override the internal Grafana health URL
  --clients N                   Concurrent clients (1..64)
  --http-requests N             HTTP requests (1..1000000)
  --s3-objects N                S3 write/read/delete cycles (1..10000)
  --pg-transactions N           PostgreSQL transactions (1..1000000)
  --vault-operations N          Vault write/read/delete cycles (1..10000)
  --dragonfly-requests N        Dragonfly SET and GET requests (1..1000000)
  --phase-timeout SECONDS       Per-phase hard stop (30..3600; default 900)
  --max-error-percent PERCENT   Allowed operation errors (0..20; default 1.0)
  --max-restart-delta N         Hard stop on added container restarts (default 10)
  --dry-run                     Plan and write evidence without cluster access
  -h, --help                    Show this help

The script fails closed when baseline/final health is bad, a node reports
pressure, a phase exceeds its timeout/error budget, or restart growth exceeds
the configured bound. Evidence is JSON plus TSV and contains no Secrets.
EOF
}

log() { printf '[tier-load] %s\n' "$*" >&2; }
fail() { printf '[tier-load] ERROR: %s\n' "$*" >&2; exit 2; }
integer_in_range() {
  local name="$1" value="$2" min="$3" max="$4"
  [[ "$value" =~ ^[0-9]+$ ]] || fail "$name must be an integer"
  (( value >= min && value <= max )) || fail "$name must be between $min and $max"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="${2:?missing config path}"; shift 2 ;;
    --kubeconfig) KUBECONFIG_FILE="${2:?missing kubeconfig path}"; shift 2 ;;
    --vault-init) VAULT_INIT_FILE="${2:?missing Vault init path}"; shift 2 ;;
    --output) OUTPUT_DIR="${2:?missing output directory}"; shift 2 ;;
    --run-id) RUN_ID="${2:?missing run ID}"; shift 2 ;;
    --http-url) HTTP_URL="${2:?missing HTTP URL}"; shift 2 ;;
    --clients) CLIENTS="${2:?missing clients}"; shift 2 ;;
    --http-requests) HTTP_REQUESTS="${2:?missing HTTP request count}"; shift 2 ;;
    --s3-objects) S3_OBJECTS="${2:?missing S3 object count}"; shift 2 ;;
    --pg-transactions) PG_TRANSACTIONS="${2:?missing PostgreSQL transaction count}"; shift 2 ;;
    --vault-operations) VAULT_OPERATIONS="${2:?missing Vault operation count}"; shift 2 ;;
    --dragonfly-requests) DRAGONFLY_REQUESTS="${2:?missing Dragonfly request count}"; shift 2 ;;
    --phase-timeout) PHASE_TIMEOUT="${2:?missing phase timeout}"; shift 2 ;;
    --max-error-percent) MAX_ERROR_PERCENT="${2:?missing error percent}"; shift 2 ;;
    --max-restart-delta) MAX_RESTART_DELTA="${2:?missing restart delta}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -f "$CONFIG_FILE" ]] || fail "config not found: $CONFIG_FILE"
run_id_pattern='^[a-z0-9]([-a-z0-9]{0,30}[a-z0-9])?$'
[[ "$RUN_ID" =~ $run_id_pattern ]] || fail "run ID must be a lowercase DNS label (maximum 32 characters)"
command -v yq >/dev/null 2>&1 || fail "yq is required"
command -v jq >/dev/null 2>&1 || fail "jq is required"

project=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
profile=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
domain=$(yq -r '.global.domain // ""' "$CONFIG_FILE")
safe_run_id=${RUN_ID//-/_}
table_name="tier_load_${safe_run_id}"
case "$profile" in
  minimal) defaults=(2 500 25 200 50 1000) ;;
  small) defaults=(4 2000 100 1000 200 5000) ;;
  medium-optimized) defaults=(8 5000 250 2500 500 10000) ;;
  medium) defaults=(12 10000 500 5000 1000 25000) ;;
  production) defaults=(20 20000 1000 10000 2000 50000) ;;
  *) defaults=(4 1000 50 500 100 2500) ;;
esac
CLIENTS=${CLIENTS:-${defaults[0]}}
HTTP_REQUESTS=${HTTP_REQUESTS:-${defaults[1]}}
S3_OBJECTS=${S3_OBJECTS:-${defaults[2]}}
PG_TRANSACTIONS=${PG_TRANSACTIONS:-${defaults[3]}}
VAULT_OPERATIONS=${VAULT_OPERATIONS:-${defaults[4]}}
DRAGONFLY_REQUESTS=${DRAGONFLY_REQUESTS:-${defaults[5]}}
HTTP_URL=${HTTP_URL:-http://grafana.monitoring.svc/api/health}

integer_in_range clients "$CLIENTS" 1 64
integer_in_range http-requests "$HTTP_REQUESTS" 1 1000000
integer_in_range s3-objects "$S3_OBJECTS" 1 10000
integer_in_range pg-transactions "$PG_TRANSACTIONS" 1 1000000
integer_in_range vault-operations "$VAULT_OPERATIONS" 1 10000
integer_in_range dragonfly-requests "$DRAGONFLY_REQUESTS" 1 1000000
integer_in_range phase-timeout "$PHASE_TIMEOUT" 30 3600
integer_in_range max-restart-delta "$MAX_RESTART_DELTA" 0 10000
[[ "$MAX_ERROR_PERCENT" =~ ^([0-9]+)(\.[0-9]{1,2})?$ ]] || fail "max-error-percent must be numeric"
max_error_bps=$(awk -v value="$MAX_ERROR_PERCENT" 'BEGIN { printf "%d", value * 100 }')
(( max_error_bps >= 0 && max_error_bps <= 2000 )) || fail "max-error-percent must be between 0 and 20"
http_url_pattern='^https?://[-a-zA-Z0-9._~:/?#%=&+]+$'
[[ "$HTTP_URL" =~ $http_url_pattern ]] || fail "unsafe HTTP URL"

stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$ROOT_DIR/cluster-backups/load-tests/${project}-${profile}-${stamp}-${RUN_ID}"
fi
mkdir -p "$OUTPUT_DIR/logs" "$OUTPUT_DIR/evidence"
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd)
PHASES_TSV="$OUTPUT_DIR/phases.tsv"
printf 'phase\tenabled\tstatus\toperations\terrors\terror_percent\tduration_seconds\tlog\n' >"$PHASES_TSV"

if [[ -z "$VAULT_INIT_FILE" ]]; then
  VAULT_INIT_FILE="$ROOT_DIR/playbooks/.vault-init-${project}.json"
fi

if ! $DRY_RUN; then
  command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
  command -v ansible-vault >/dev/null 2>&1 || fail "ansible-vault is required"
  if [[ -n "$KUBECONFIG_FILE" ]]; then
    [[ -f "$KUBECONFIG_FILE" ]] || fail "kubeconfig not found: $KUBECONFIG_FILE"
    K=(kubectl --kubeconfig "$KUBECONFIG_FILE" --request-timeout=30s)
    K_VAULT=(kubectl --kubeconfig "$KUBECONFIG_FILE" --request-timeout="${PHASE_TIMEOUT}s")
  else
    K=(kubectl --request-timeout=30s)
    K_VAULT=(kubectl --request-timeout="${PHASE_TIMEOUT}s")
  fi
else
  K=(kubectl)
  K_VAULT=(kubectl)
fi

enabled() { [[ "$(yq -r "$1 // false" "$CONFIG_FILE")" == true ]]; }
phase_is_enabled() {
  case "$1" in
    http) enabled '.observability.enabled' && enabled '.observability.grafana.enabled' ;;
    s3) enabled '.storage.enabled' ;;
    postgresql) enabled '.databases.enabled' && enabled '.databases.postgresql.enabled' ;;
    vault) enabled '.secrets.enabled' ;;
    dragonfly) enabled '.dragonfly.enabled' ;;
  esac
}

active_job_ns=""
active_job_name=""
s3_started=false
pg_started=false
vault_started=false
dragonfly_started=false
vault_token=""

cleanup_job() {
  local namespace="$active_job_ns" name="$active_job_name"
  if [[ -n "$active_job_ns" && -n "$active_job_name" ]] && ! $DRY_RUN; then
    if ! "${K[@]}" delete job -n "$namespace" "$name" --ignore-not-found --wait=true --timeout=30s >/dev/null 2>&1; then
      log "cleanup failed: could not delete job $namespace/$name"
      return 1
    fi
    if "${K[@]}" get job -n "$namespace" "$name" >/dev/null 2>&1; then
      log "cleanup failed: job still exists $namespace/$name"
      return 1
    fi
  fi
  active_job_ns=""; active_job_name=""
}

cleanup_s3() {
  $s3_started || return 0
  local pod="tier-load-s3-clean-${RUN_ID}"
  "${K[@]}" delete pod -n storage "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  "${K[@]}" run "$pod" -n storage --restart=Never --image="$S3_IMAGE" \
    --overrides="$(jq -cn --arg name "$pod" --arg run "$RUN_ID" --arg image "$S3_IMAGE" '{
      spec:{restartPolicy:"Never",automountServiceAccountToken:false,securityContext:{runAsNonRoot:true,runAsUser:1000,runAsGroup:1000,seccompProfile:{type:"RuntimeDefault"}},
      containers:[{name:$name,image:$image,envFrom:[{secretRef:{name:"seaweedfs-s3-config"}}],
      env:[{name:"AWS_DEFAULT_REGION",value:"us-east-1"},{name:"AWS_EC2_METADATA_DISABLED",value:"true"}],
      securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},
      command:["/bin/sh","-ec"],args:["set -e; endpoint=http://seaweedfs-filer.storage.svc.cluster.local:8333; prefix=s3://backups/tier-load/"+$run+"; aws --endpoint-url $endpoint s3 rm $prefix --recursive; test -z \"$(aws --endpoint-url $endpoint s3 ls $prefix --recursive)\""]}]}}')" \
    >/dev/null 2>&1 || return 1
  "${K[@]}" wait -n storage --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=3m >/dev/null 2>&1 || return 1
  "${K[@]}" delete pod -n storage "$pod" --wait=false >/dev/null 2>&1 || true
}

cleanup_postgresql() {
  $pg_started || return 0
  local primary
  primary=$("${K[@]}" get pods -n databases -l postgres-operator.crunchydata.com/role=primary \
    -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
  [[ -n "$primary" ]] || return 1
  "${K[@]}" exec -n databases "$primary" -c database -- psql -U postgres -d myapp \
    -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS ${table_name}" >/dev/null 2>&1
}

cleanup_vault() {
  $vault_started || return 0
  [[ -n "$vault_token" ]] || return 1
  local index output rc=0
  for ((index=1; index<=CLIENTS; index++)); do
    output=$("${K[@]}" exec -n vault vault-0 -- env VAULT_TOKEN="$vault_token" \
      vault kv metadata delete "secret/tier-load/${RUN_ID}/${index}" 2>&1) || {
        grep -q 'No value found' <<<"$output" || rc=1
      }
    output=$("${K[@]}" exec -n vault vault-0 -- env VAULT_TOKEN="$vault_token" \
      vault kv metadata get "secret/tier-load/${RUN_ID}/${index}" 2>&1) && rc=1
    if [[ "$rc" -eq 0 ]] && ! grep -q 'No value found' <<<"$output"; then
      rc=1
    fi
  done
  [[ "$rc" -eq 0 ]] || log "cleanup failed: Vault metadata remains or could not be verified"
  return "$rc"
}

cleanup_dragonfly() {
  $dragonfly_started || return 0
  local pod="tier-load-df-clean-${RUN_ID}"
  "${K[@]}" delete pod -n dragonfly "$pod" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  "${K[@]}" run "$pod" -n dragonfly --restart=Never --image="$DRAGONFLY_IMAGE" \
    --overrides="$(jq -cn --arg name "$pod" --arg run "$RUN_ID" --arg image "$DRAGONFLY_IMAGE" '{
      spec:{restartPolicy:"Never",automountServiceAccountToken:false,securityContext:{runAsNonRoot:true,runAsUser:999,runAsGroup:999,seccompProfile:{type:"RuntimeDefault"}},
      containers:[{name:$name,image:$image,env:[{name:"PASSWORD",valueFrom:{secretKeyRef:{name:"dragonfly-auth",key:"password"}}}],
      securityContext:{allowPrivilegeEscalation:false,capabilities:{drop:["ALL"]}},command:["/bin/sh","-ec"],
      args:["export REDISCLI_AUTH=$PASSWORD; pattern=tier-load:"+$run+":*; redis-cli -h dragonfly --scan --pattern $pattern >/tmp/keys; while IFS= read -r key; do [ -z \"$key\" ] || redis-cli -h dragonfly del \"$key\" >/dev/null; done </tmp/keys; redis-cli -h dragonfly --scan --pattern $pattern >/tmp/remaining; test ! -s /tmp/remaining"]}]}}')" \
    >/dev/null 2>&1 || return 1
  "${K[@]}" wait -n dragonfly --for=jsonpath='{.status.phase}'=Succeeded "pod/$pod" --timeout=3m >/dev/null 2>&1 || return 1
  "${K[@]}" delete pod -n dragonfly "$pod" --wait=false >/dev/null 2>&1 || true
}

cleanup_all() {
  local rc=0
  cleanup_job || rc=1
  if ! $DRY_RUN; then
    cleanup_s3 || rc=1
    cleanup_postgresql || rc=1
    cleanup_vault || rc=1
    cleanup_dragonfly || rc=1
  fi
  vault_token=""
  return "$rc"
}
on_signal() {
  local status="$1"
  trap - EXIT INT TERM
  cleanup_all || true
  exit "$status"
}
trap 'cleanup_all || true' EXIT
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

collect_evidence() {
  local stage="$1" args
  local dir="$OUTPUT_DIR/evidence/$stage"
  args=(--config "$CONFIG_FILE" --output "$dir" --stage "$stage")
  [[ -n "$KUBECONFIG_FILE" ]] && args+=(--kubeconfig "$KUBECONFIG_FILE")
  $DRY_RUN && args+=(--dry-run)
  "$SCRIPT_DIR/collect-live-evidence.sh" "${args[@]}" >/dev/null
  printf '%s/evidence.json' "$dir"
}

assert_cluster_safe() {
  local evidence="$1" baseline_restarts="$2" healthy restarts delta
  $DRY_RUN && return 0
  healthy=$(jq -r '.healthy' "$evidence")
  [[ "$healthy" == true ]] || {
    jq '{stage,healthy,nodes,pods,controllers,storage,networking}' "$evidence" >&2
    log "hard stop: cluster health gate failed"
    return 1
  }
  restarts=$(jq -r '.pods.restarts' "$evidence")
  delta=$((restarts - baseline_restarts))
  (( delta <= MAX_RESTART_DELTA )) || {
    log "hard stop: restart delta $delta exceeds $MAX_RESTART_DELTA"
    return 1
  }
}

execute_job() {
  local namespace="$1" name="$2" manifest="$3" log_file="$4"
  active_job_ns="$namespace"; active_job_name="$name"
  "${K[@]}" apply -f "$manifest" >/dev/null
  local deadline=$(( $(date +%s) + PHASE_TIMEOUT )) complete failed
  while (( $(date +%s) < deadline )); do
    complete=$("${K[@]}" get job -n "$namespace" "$name" -o jsonpath='{.status.succeeded}' 2>/dev/null || true)
    failed=$("${K[@]}" get job -n "$namespace" "$name" -o jsonpath='{.status.failed}' 2>/dev/null || true)
    if [[ "${complete:-0}" -ge 1 ]]; then break; fi
    if [[ "${failed:-0}" -ge 1 ]]; then
      "${K[@]}" logs -n "$namespace" "job/$name" --all-containers=true >"$log_file" 2>&1 || true
      "${K[@]}" describe job -n "$namespace" "$name" >>"$log_file" 2>&1 || true
      cleanup_job
      return 1
    fi
    sleep 5
  done
  if [[ "${complete:-0}" -lt 1 ]]; then
    "${K[@]}" logs -n "$namespace" "job/$name" --all-containers=true >"$log_file" 2>&1 || true
    printf 'hard stop: phase timeout after %ss\n' "$PHASE_TIMEOUT" >>"$log_file"
    cleanup_job
    return 1
  fi
  "${K[@]}" logs -n "$namespace" "job/$name" --all-containers=true >"$log_file" 2>&1
  if ! cleanup_job; then
    log "load Job cleanup failed: $namespace/$name"
    return 1
  fi
}

result_from_log() {
  local log_file="$1" line operations errors
  line=$(grep '^RESULT ' "$log_file" | tail -1) || return 1
  operations=$(sed -n 's/.* operations=\([0-9][0-9]*\).*/\1/p' <<<"$line")
  errors=$(sed -n 's/.* errors=\([0-9][0-9]*\).*/\1/p' <<<"$line")
  [[ -n "$operations" && -n "$errors" ]] || return 1
  printf '%s\t%s\n' "$operations" "$errors"
}

phase_http() {
  local manifest="$OUTPUT_DIR/http-job.yaml" log_file="$OUTPUT_DIR/logs/http.log" name="tier-load-http-${RUN_ID}"
  cat >"$manifest" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
  namespace: monitoring
  labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $PHASE_TIMEOUT
  ttlSecondsAfterFinished: 300
  template:
    metadata:
      labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 100, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: http
          image: $HTTP_IMAGE
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
          resources: {requests: {cpu: 50m, memory: 32Mi}, limits: {cpu: "1", memory: 256Mi}}
          env:
            - {name: URL, value: "$HTTP_URL"}
            - {name: REQUESTS, value: "$HTTP_REQUESTS"}
            - {name: CLIENTS, value: "$CLIENTS"}
            - {name: MAX_ERROR_BPS, value: "$max_error_bps"}
          command: ["/bin/sh", "-ec"]
          args:
            - |
              worker() {
                id="\$1"; count="\$2"; ok=0; errors=0; i=0
                while [ "\$i" -lt "\$count" ]; do
                  code=\$(curl -ksS --connect-timeout 5 --max-time 20 -o /dev/null -w '%{http_code}' "\$URL" || printf 000)
                  case "\$code" in 2??|3??) ok=\$((ok+1)) ;; *) errors=\$((errors+1)) ;; esac
                  i=\$((i+1))
                done
                printf '%s %s\n' "\$ok" "\$errors" >"/tmp/result.\$id"
              }
              base=\$((REQUESTS/CLIENTS)); remainder=\$((REQUESTS%CLIENTS)); client=1
              while [ "\$client" -le "\$CLIENTS" ]; do
                count="\$base"; [ "\$client" -le "\$remainder" ] && count=\$((count+1))
                worker "\$client" "\$count" & client=\$((client+1))
              done
              wait
              ok=0; errors=0
              for result in /tmp/result.*; do set -- \$(cat "\$result"); ok=\$((ok+\$1)); errors=\$((errors+\$2)); done
              operations=\$((ok+errors))
              printf 'RESULT phase=http operations=%s errors=%s\n' "\$operations" "\$errors"
              [ "\$((errors*10000))" -le "\$((operations*MAX_ERROR_BPS))" ]
EOF
  execute_job monitoring "$name" "$manifest" "$log_file"
}

phase_s3() {
  local manifest="$OUTPUT_DIR/s3-job.yaml" log_file="$OUTPUT_DIR/logs/s3.log" name="tier-load-s3-${RUN_ID}"
  s3_started=true
  cat >"$manifest" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
  namespace: storage
  labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $PHASE_TIMEOUT
  template:
    metadata: {labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 1000, runAsGroup: 1000, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: s3
          image: $S3_IMAGE
          envFrom: [{secretRef: {name: seaweedfs-s3-config}}]
          env:
            - {name: AWS_DEFAULT_REGION, value: us-east-1}
            - {name: AWS_EC2_METADATA_DISABLED, value: "true"}
            - {name: OBJECTS, value: "$S3_OBJECTS"}
            - {name: CLIENTS, value: "$CLIENTS"}
            - {name: RUN_ID, value: "$RUN_ID"}
            - {name: MAX_ERROR_BPS, value: "$max_error_bps"}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
          resources: {requests: {cpu: 50m, memory: 64Mi}, limits: {cpu: "1", memory: 512Mi}}
          command: ["/bin/sh", "-ec"]
          args:
            - |
              endpoint=http://seaweedfs-filer.storage.svc.cluster.local:8333
              worker() {
                id="\$1"; count="\$2"; errors=0; i=1
                while [ "\$i" -le "\$count" ]; do
                  object=\$((id+(i-1)*CLIENTS)); key="s3://backups/tier-load/\$RUN_ID/object-\$object"
                  value="/tmp/value.\$id"; readback="/tmp/read.\$id"
                  printf '%s:%s' "\$RUN_ID" "\$object" >"\$value"; rm -f "\$readback"
                  aws --endpoint-url "\$endpoint" s3 cp "\$value" "\$key" >/dev/null || errors=\$((errors+1))
                  aws --endpoint-url "\$endpoint" s3 cp "\$key" "\$readback" >/dev/null || errors=\$((errors+1))
                  cmp -s "\$value" "\$readback" || errors=\$((errors+1))
                  aws --endpoint-url "\$endpoint" s3 rm "\$key" >/dev/null || errors=\$((errors+1))
                  i=\$((i+1))
                done
                printf '%s\n' "\$errors" >"/tmp/s3-result.\$id"
              }
              base=\$((OBJECTS/CLIENTS)); remainder=\$((OBJECTS%CLIENTS)); client=1
              while [ "\$client" -le "\$CLIENTS" ]; do
                count="\$base"; [ "\$client" -le "\$remainder" ] && count=\$((count+1))
                worker "\$client" "\$count" & client=\$((client+1))
              done
              wait; errors=0
              for result in /tmp/s3-result.*; do errors=\$((errors+\$(cat "\$result"))); done
              operations=\$((OBJECTS*4))
              printf 'RESULT phase=s3 operations=%s errors=%s\n' "\$operations" "\$errors"
              [ "\$((errors*10000))" -le "\$((operations*MAX_ERROR_BPS))" ]
EOF
  execute_job storage "$name" "$manifest" "$log_file"
}

phase_postgresql() {
  local manifest="$OUTPUT_DIR/postgresql-job.yaml" log_file="$OUTPUT_DIR/logs/postgresql.log" name="tier-load-pg-${RUN_ID}"
  pg_started=true
  cat >"$manifest" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
  namespace: databases
  labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $PHASE_TIMEOUT
  template:
    metadata: {labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: pgbench
          image: $POSTGRES_IMAGE
          env:
            - {name: PGHOST, valueFrom: {secretKeyRef: {name: ${project}-pg-pguser-myapp, key: host}}}
            - {name: PGPORT, valueFrom: {secretKeyRef: {name: ${project}-pg-pguser-myapp, key: port}}}
            - {name: PGUSER, valueFrom: {secretKeyRef: {name: ${project}-pg-pguser-myapp, key: user}}}
            - {name: PGPASSWORD, valueFrom: {secretKeyRef: {name: ${project}-pg-pguser-myapp, key: password}}}
            - {name: PGDATABASE, valueFrom: {secretKeyRef: {name: ${project}-pg-pguser-myapp, key: dbname}}}
            - {name: TRANSACTIONS, value: "$PG_TRANSACTIONS"}
            - {name: CLIENTS, value: "$CLIENTS"}
            - {name: TABLE_NAME, value: "$table_name"}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
          resources: {requests: {cpu: 100m, memory: 64Mi}, limits: {cpu: "2", memory: 512Mi}}
          command: ["/bin/sh", "-ec"]
          args:
            - |
              cleanup() { psql -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS \$TABLE_NAME" >/dev/null || true; }
              trap cleanup EXIT
              psql -v ON_ERROR_STOP=1 -c "DROP TABLE IF EXISTS \$TABLE_NAME; CREATE UNLOGGED TABLE \$TABLE_NAME(id bigserial PRIMARY KEY, value text NOT NULL)"
              printf 'INSERT INTO %s(value) VALUES (md5(random()::text));\nSELECT value FROM %s ORDER BY id DESC LIMIT 1;\n' "\$TABLE_NAME" "\$TABLE_NAME" >/tmp/load.sql
              per_client=\$(((TRANSACTIONS+CLIENTS-1)/CLIENTS))
              pgbench -n -c "\$CLIENTS" -j "\$CLIENTS" -t "\$per_client" -f /tmp/load.sql | tee /tmp/pgbench.out
              processed=\$(sed -n 's/^number of transactions actually processed: \([0-9][0-9]*\).*/\1/p' /tmp/pgbench.out | tail -1)
              [ -n "\$processed" ]
              printf 'RESULT phase=postgresql operations=%s errors=0\n' "\$processed"
EOF
  execute_job databases "$name" "$manifest" "$log_file"
}

phase_vault() {
  local log_file="$OUTPUT_DIR/logs/vault.log" actual_image
  vault_started=true
  actual_image=$("${K[@]}" get pod -n vault vault-0 -o jsonpath='{.spec.containers[?(@.name=="vault")].image}')
  [[ "$actual_image" == "$VAULT_IMAGE" ]] || {
    log "refusing Vault load against unpinned image: expected=$VAULT_IMAGE actual=$actual_image"
    return 1
  }
  [[ -f "$VAULT_INIT_FILE" ]] || { log "encrypted Vault init material not found: $VAULT_INIT_FILE"; return 1; }
  [[ -n "${ANSIBLE_VAULT_PASSWORD_FILE:-}" ]] || { log "ANSIBLE_VAULT_PASSWORD_FILE is required"; return 1; }
  vault_token=$(ansible-vault view --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE" "$VAULT_INIT_FILE" | jq -r .root_token)
  [[ -n "$vault_token" && "$vault_token" != null ]] || return 1
  # shellcheck disable=SC2016 # The program expands only inside the Vault pod.
  "${K_VAULT[@]}" exec -n vault vault-0 -- env VAULT_TOKEN="$vault_token" RUN_ID="$RUN_ID" \
    OPERATIONS="$VAULT_OPERATIONS" CLIENTS="$CLIENTS" MAX_ERROR_BPS="$max_error_bps" sh -ec '
      worker() {
        id="$1"; count="$2"; errors=0; i=1; path="secret/tier-load/$RUN_ID/$id"
        while [ "$i" -le "$count" ]; do
          value="$RUN_ID:$id:$i"
          vault kv put "$path" value="$value" >/dev/null || errors=$((errors+1))
          actual=$(vault kv get -field=value "$path" 2>/dev/null || true)
          [ "$actual" = "$value" ] || errors=$((errors+1))
          vault kv delete "$path" >/dev/null || errors=$((errors+1))
          i=$((i+1))
        done
        vault kv metadata delete "$path" >/dev/null || true
        printf "%s\n" "$errors" >"/tmp/vault-result.$id"
      }
      base=$((OPERATIONS/CLIENTS)); remainder=$((OPERATIONS%CLIENTS)); client=1
      while [ "$client" -le "$CLIENTS" ]; do
        count="$base"; [ "$client" -le "$remainder" ] && count=$((count+1))
        worker "$client" "$count" & client=$((client+1))
      done
      wait; errors=0
      for result in /tmp/vault-result.*; do errors=$((errors+$(cat "$result"))); done
      total=$((OPERATIONS*3))
      printf "RESULT phase=vault operations=%s errors=%s\n" "$total" "$errors"
      [ "$((errors*10000))" -le "$((total*MAX_ERROR_BPS))" ]
    ' >"$log_file" 2>&1
}

phase_dragonfly() {
  local manifest="$OUTPUT_DIR/dragonfly-job.yaml" log_file="$OUTPUT_DIR/logs/dragonfly.log" name="tier-load-df-${RUN_ID}"
  dragonfly_started=true
  cat >"$manifest" <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: $name
  namespace: dragonfly
  labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}
spec:
  backoffLimit: 0
  activeDeadlineSeconds: $PHASE_TIMEOUT
  template:
    metadata: {labels: {app.kubernetes.io/name: tier-load-test, tier-load-run: "$RUN_ID"}}
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext: {runAsNonRoot: true, runAsUser: 999, runAsGroup: 999, seccompProfile: {type: RuntimeDefault}}
      containers:
        - name: redis-benchmark
          image: $DRAGONFLY_IMAGE
          env:
            - {name: PASSWORD, valueFrom: {secretKeyRef: {name: dragonfly-auth, key: password}}}
            - {name: REQUESTS, value: "$DRAGONFLY_REQUESTS"}
            - {name: CLIENTS, value: "$CLIENTS"}
            - {name: RUN_ID, value: "$RUN_ID"}
          securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: ["ALL"]}}
          resources: {requests: {cpu: 50m, memory: 32Mi}, limits: {cpu: "1", memory: 256Mi}}
          command: ["/bin/sh", "-ec"]
          args:
            - |
              export REDISCLI_AUTH="\$PASSWORD"
              redis-benchmark -h dragonfly -n "\$REQUESTS" -c "\$CLIENTS" -r "\$REQUESTS" -q SET "tier-load:\$RUN_ID:__rand_int__" value
              redis-benchmark -h dragonfly -n "\$REQUESTS" -c "\$CLIENTS" -r "\$REQUESTS" -q GET "tier-load:\$RUN_ID:__rand_int__"
              pattern="tier-load:\$RUN_ID:*"
              redis-cli -h dragonfly --scan --pattern "\$pattern" >/tmp/keys
              while IFS= read -r key; do
                [ -z "\$key" ] || redis-cli -h dragonfly del "\$key" >/dev/null
              done </tmp/keys
              redis-cli -h dragonfly --scan --pattern "\$pattern" >/tmp/remaining
              test ! -s /tmp/remaining
              printf 'RESULT phase=dragonfly operations=%s errors=0\n' "\$((REQUESTS*2))"
EOF
  execute_job dragonfly "$name" "$manifest" "$log_file"
}

run_phase() {
  local phase="$1" function="$2" start end duration result operations errors percent log_file evidence
  log_file="$OUTPUT_DIR/logs/${phase}.log"
  if ! phase_is_enabled "$phase"; then
    printf '%s\tfalse\tskipped\t0\t0\t0.00\t0\t%s\n' "$phase" "$log_file" >>"$PHASES_TSV"
    log "$phase skipped by profile"
    return 0
  fi
  if $DRY_RUN; then
    printf '%s\ttrue\tplanned\t0\t0\t0.00\t0\t%s\n' "$phase" "$log_file" >>"$PHASES_TSV"
    log "$phase planned"
    return 0
  fi
  start=$(date +%s)
  if ! "$function"; then
    end=$(date +%s); duration=$((end-start))
    printf '%s\ttrue\tfailed\t0\t1\t100.00\t%s\t%s\n' "$phase" "$duration" "$log_file" >>"$PHASES_TSV"
    return 1
  fi
  result=$(result_from_log "$log_file") || {
    printf '%s\ttrue\tfailed\t0\t1\t100.00\t0\t%s\n' "$phase" "$log_file" >>"$PHASES_TSV"
    return 1
  }
  IFS=$'\t' read -r operations errors <<<"$result"
  percent=$(awk -v e="$errors" -v n="$operations" 'BEGIN { if (n==0) print "100.00"; else printf "%.2f", e*100/n }')
  if ! cleanup_job; then
    log "load Job cleanup failed for phase $phase"
    return 1
  fi
  case "$phase" in
    s3)
      if ! cleanup_s3; then log "S3 cleanup failed"; return 1; fi
      s3_started=false
      ;;
    postgresql)
      if ! cleanup_postgresql; then log "PostgreSQL cleanup failed"; return 1; fi
      pg_started=false
      ;;
    vault)
      if ! cleanup_vault; then log "Vault cleanup failed"; return 1; fi
      vault_started=false; vault_token=""
      ;;
    dragonfly)
      if ! cleanup_dragonfly; then log "Dragonfly cleanup failed"; return 1; fi
      dragonfly_started=false
      ;;
  esac
  end=$(date +%s); duration=$((end-start))
  printf '%s\ttrue\tpassed\t%s\t%s\t%s\t%s\t%s\n' \
    "$phase" "$operations" "$errors" "$percent" "$duration" "$log_file" >>"$PHASES_TSV"
  evidence=$(collect_evidence "$phase")
  assert_cluster_safe "$evidence" "$baseline_restarts"
}

write_summary() {
  local overall="$1" final_evidence="${2:-}" phases_json="$OUTPUT_DIR/phases.json"
  jq -Rn '[inputs | split("\t") | select(.[0] != "phase") | {
    phase:.[0],enabled:(.[1]=="true"),status:.[2],operations:(.[3]|tonumber),
    errors:(.[4]|tonumber),error_percent:(.[5]|tonumber),duration_seconds:(.[6]|tonumber),log:.[7]}]' \
    <"$PHASES_TSV" >"$phases_json"
  jq -n --arg schema tier-load-test/v1 --arg run_id "$RUN_ID" --arg project "$project" \
    --arg profile "$profile" --arg domain "$domain" --arg status "$overall" \
    --arg config "$CONFIG_FILE" --arg kubeconfig "${KUBECONFIG_FILE:-active}" \
    --arg final_evidence "$final_evidence" --argjson dry_run "$DRY_RUN" \
    --argjson clients "$CLIENTS" --argjson max_error_bps "$max_error_bps" \
    --argjson timeout "$PHASE_TIMEOUT" --argjson max_restart_delta "$MAX_RESTART_DELTA" \
    --slurpfile phases "$phases_json" \
    '{schema:$schema,run_id:$run_id,project:$project,profile:$profile,domain:$domain,status:$status,
      dry_run:$dry_run,config:$config,kubeconfig:$kubeconfig,
      limits:{clients:$clients,max_error_percent:($max_error_bps/100),phase_timeout_seconds:$timeout,
        max_restart_delta:$max_restart_delta},images:{http:"curlimages/curl:8.17.0",
        s3:"amazon/aws-cli:2.34.48",postgresql:"postgres:18.2-alpine3.23",
        vault:"hashicorp/vault:2.0.3",dragonfly:"redis:7.4.7-alpine3.21"},
      phases:$phases[0],final_evidence:(if $final_evidence=="" then null else $final_evidence end)}' \
    >"$OUTPUT_DIR/summary.json"
}

log "profile=$profile project=$project clients=$CLIENTS output=$OUTPUT_DIR"
baseline=$(collect_evidence baseline)
if $DRY_RUN; then
  baseline_restarts=0
else
  baseline_restarts=$(jq -r '.pods.restarts' "$baseline")
  assert_cluster_safe "$baseline" "$baseline_restarts" || { write_summary failed "$baseline"; exit 1; }
fi

if $DRY_RUN; then overall=planned; else overall=passed; fi
for phase_spec in 'http phase_http' 's3 phase_s3' 'postgresql phase_postgresql' 'vault phase_vault' 'dragonfly phase_dragonfly'; do
  read -r phase function <<<"$phase_spec"
  if ! run_phase "$phase" "$function"; then
    overall=failed
    log "hard stop after failed phase: $phase"
    break
  fi
done

final_evidence=$(collect_evidence final)
if ! $DRY_RUN && ! assert_cluster_safe "$final_evidence" "$baseline_restarts"; then
  overall=failed
fi
if ! cleanup_all; then
  overall=failed
  log "cleanup verification failed"
fi
write_summary "$overall" "$final_evidence"
trap - EXIT INT TERM

log "summary: $OUTPUT_DIR/summary.json"
[[ "$overall" == passed || "$overall" == planned ]] || exit 1
