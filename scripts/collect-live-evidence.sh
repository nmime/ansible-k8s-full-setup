#!/usr/bin/env bash
# Collect a secret-free, machine-readable snapshot of live cluster health.
# shellcheck disable=SC2129 # Deliberate one-query-per-resource TSV assembly.
set -Eeuo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT_DIR=$(cd "$SCRIPT_DIR/.." && pwd)
CONFIG_FILE="$ROOT_DIR/platform-orchestrator/platform.yaml"
KUBECONFIG_FILE="${KUBECONFIG:-}"
OUTPUT_DIR=""
STAGE="snapshot"
DRY_RUN=false

usage() {
  cat <<'EOF'
Usage: collect-live-evidence.sh [OPTIONS]

Options:
  --config FILE       Profile YAML used to label the evidence
  --kubeconfig FILE   Explicit kubeconfig; never changes the active context
  --output DIR        Evidence directory (default: cluster-backups/load-tests/...)
  --stage NAME        Safe evidence label such as baseline, http, or final
  --dry-run           Write a plan without contacting Kubernetes
  -h, --help          Show this help

Writes evidence.json, resources.tsv, top-nodes.tsv, top-pods.tsv, and
warning-events.tsv. Kubernetes Secrets and manifest bodies are never captured.
EOF
}

fail() { printf '[live-evidence] ERROR: %s\n' "$*" >&2; exit 2; }
log() { printf '[live-evidence] %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="${2:?missing config path}"; shift 2 ;;
    --kubeconfig) KUBECONFIG_FILE="${2:?missing kubeconfig path}"; shift 2 ;;
    --output) OUTPUT_DIR="${2:?missing output directory}"; shift 2 ;;
    --stage) STAGE="${2:?missing stage}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown argument: $1" ;;
  esac
done

[[ -f "$CONFIG_FILE" ]] || fail "config not found: $CONFIG_FILE"
[[ "$STAGE" =~ ^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}$ ]] || fail "invalid stage: $STAGE"
command -v yq >/dev/null 2>&1 || fail "yq is required"
command -v jq >/dev/null 2>&1 || fail "jq is required"

project=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
profile=$(yq -r '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
expected_nodes=$(yq -r '(.infrastructure.control_plane.count // 0) + (.infrastructure.workers.count // 0)' "$CONFIG_FILE")
timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)
stamp=$(date -u +%Y%m%dT%H%M%SZ)
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$ROOT_DIR/cluster-backups/load-tests/${project}-${stamp}/${STAGE}"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR=$(cd "$OUTPUT_DIR" && pwd)

if $DRY_RUN; then
  jq -n \
    --arg schema "tier-live-evidence/v1" \
    --arg collected_at "$timestamp" \
    --arg stage "$STAGE" \
    --arg project "$project" \
    --arg profile "$profile" \
    --argjson expected_nodes "$expected_nodes" \
    '{schema:$schema,collected_at:$collected_at,stage:$stage,project:$project,
      profile:$profile,dry_run:true,expected_nodes:$expected_nodes,
      planned_files:["resources.tsv","top-nodes.tsv","top-pods.tsv","warning-events.tsv"]}' \
    >"$OUTPUT_DIR/evidence.json"
  printf 'kind\tnamespace\tname\tready\tdesired\trestarts\tphase\n' >"$OUTPUT_DIR/resources.tsv"
  : >"$OUTPUT_DIR/top-nodes.tsv"
  : >"$OUTPUT_DIR/top-pods.tsv"
  : >"$OUTPUT_DIR/warning-events.tsv"
  log "dry-run evidence plan: $OUTPUT_DIR/evidence.json"
  printf '%s\n' "$OUTPUT_DIR/evidence.json"
  exit 0
fi

command -v kubectl >/dev/null 2>&1 || fail "kubectl is required"
if [[ -n "$KUBECONFIG_FILE" ]]; then
  [[ -f "$KUBECONFIG_FILE" ]] || fail "kubeconfig not found: $KUBECONFIG_FILE"
  K=(kubectl --kubeconfig "$KUBECONFIG_FILE" --request-timeout=30s)
else
  K=(kubectl --request-timeout=30s)
fi

"${K[@]}" get --raw=/readyz >/dev/null || fail "Kubernetes API is not ready"
context=$("${K[@]}" config current-context 2>/dev/null || printf unknown)
server=$("${K[@]}" config view --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || printf unknown)

tmp_dir=$(mktemp -d "${TMPDIR:-/tmp}/tier-live-evidence.XXXXXX")
trap 'rm -rf "$tmp_dir"' EXIT

"${K[@]}" get nodes -o json >"$tmp_dir/nodes.json"
"${K[@]}" get pods -A -o json >"$tmp_dir/pods.json"
"${K[@]}" get deployments -A -o json >"$tmp_dir/deployments.json"
"${K[@]}" get statefulsets -A -o json >"$tmp_dir/statefulsets.json"
"${K[@]}" get daemonsets -A -o json >"$tmp_dir/daemonsets.json"
"${K[@]}" get jobs -A -o json >"$tmp_dir/jobs.json"
"${K[@]}" get pvc -A -o json >"$tmp_dir/pvcs.json"
"${K[@]}" get apiservices.apiregistration.k8s.io -o json >"$tmp_dir/apiservices.json"

if "${K[@]}" api-resources --api-group=cert-manager.io -o name 2>/dev/null \
  | grep -qx 'certificates.cert-manager.io'; then
  "${K[@]}" get certificates -A -o json >"$tmp_dir/certificates.json"
else
  printf '{"items":[]}' >"$tmp_dir/certificates.json"
fi
if "${K[@]}" api-resources --api-group=gateway.networking.k8s.io -o name 2>/dev/null \
  | grep -qx 'httproutes.gateway.networking.k8s.io'; then
  "${K[@]}" get httproutes -A -o json >"$tmp_dir/httproutes.json"
  "${K[@]}" get services -n cilium-system \
    -l io.cilium.gateway/owning-gateway=main-gateway -o json \
    >"$tmp_dir/gateway-services.json"
else
  printf '{"items":[]}' >"$tmp_dir/httproutes.json"
  printf '{"items":[]}' >"$tmp_dir/gateway-services.json"
fi

gateway_edge=$(jq -c '
  (.items[0].spec.ports // []) as $ports
  | {service:(.items[0].metadata.name // null),
     http_node_port:([$ports[] | select(.port==80) | .nodePort][0] // null),
     https_node_port:([$ports[] | select(.port==443) | .nodePort][0] // null)}
  | .valid=((.http_node_port // 0) >= 30000 and (.http_node_port // 0) <= 32767
    and (.https_node_port // 0) >= 30000 and (.https_node_port // 0) <= 32767)' \
  "$tmp_dir/gateway-services.json")
provider_edge='{"checked":false,"present":false,"healthy":null}'
if command -v hcloud >/dev/null 2>&1 && [[ -n "${HCLOUD_TOKEN:-}" ]] \
  && hcloud load-balancer describe "${project}-lb" -o json >"$tmp_dir/load-balancer.json" 2>/dev/null; then
  provider_edge=$(jq -c --argjson gateway "$gateway_edge" '
    [.services[] | select(.listen_port==80)][0] as $http
    | [.services[] | select(.listen_port==443)][0] as $https
    | [.targets[].health_status[]
       | select(.listen_port==80 or .listen_port==443)] as $checks
    | {checked:true,present:true,
       http_destination_port:($http.destination_port // null),
       https_destination_port:($https.destination_port // null),
       checks:($checks|length),
       healthy_checks:([$checks[] | select(.status=="healthy")]|length)}
    | .ports_match=(.http_destination_port==$gateway.http_node_port
      and .https_destination_port==$gateway.https_node_port)
    | .healthy=(.ports_match and .checks>0 and .healthy_checks==.checks)' \
    "$tmp_dir/load-balancer.json")
fi

jq -r '
  ["kind","namespace","name","ready","desired","restarts","phase"],
  (.items[] | ["Node","-",.metadata.name,
    ([.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length),1,0,"-"])
  | @tsv' "$tmp_dir/nodes.json" >"$OUTPUT_DIR/resources.tsv"
jq -r '.items[] | ["Pod",.metadata.namespace,.metadata.name,
    ([.status.containerStatuses[]? | select(.ready==true)] | length),
    ((.status.containerStatuses // []) | length),
    ([.status.containerStatuses[]?.restartCount] | add // 0),.status.phase] | @tsv' \
  "$tmp_dir/pods.json" >>"$OUTPUT_DIR/resources.tsv"
jq -r '.items[] | ["Deployment",.metadata.namespace,.metadata.name,
    (.status.availableReplicas // 0),(.spec.replicas // 0),0,"-"] | @tsv' \
  "$tmp_dir/deployments.json" >>"$OUTPUT_DIR/resources.tsv"
jq -r '.items[] | ["StatefulSet",.metadata.namespace,.metadata.name,
    (.status.readyReplicas // 0),(.spec.replicas // 0),0,"-"] | @tsv' \
  "$tmp_dir/statefulsets.json" >>"$OUTPUT_DIR/resources.tsv"
jq -r '.items[] | ["DaemonSet",.metadata.namespace,.metadata.name,
    (.status.numberReady // 0),(.status.desiredNumberScheduled // 0),0,"-"] | @tsv' \
  "$tmp_dir/daemonsets.json" >>"$OUTPUT_DIR/resources.tsv"

printf 'name\tcpu\tmemory\n' >"$OUTPUT_DIR/top-nodes.tsv"
"${K[@]}" top nodes --no-headers 2>/dev/null \
  | awk 'BEGIN{OFS="\t"} {print $1,$2,$4}' >>"$OUTPUT_DIR/top-nodes.tsv" || true
printf 'namespace\tname\tcpu\tmemory\n' >"$OUTPUT_DIR/top-pods.tsv"
"${K[@]}" top pods -A --containers --no-headers 2>/dev/null \
  | awk 'BEGIN{OFS="\t"} {print $1,$2 "/" $3,$4,$5}' >>"$OUTPUT_DIR/top-pods.tsv" || true
printf 'namespace\tlast_seen\treason\tobject\tmessage\n' >"$OUTPUT_DIR/warning-events.tsv"
"${K[@]}" get events -A --field-selector type=Warning -o json 2>/dev/null \
  | jq -r '.items[] | [.metadata.namespace,
    (.eventTime // .lastTimestamp // .metadata.creationTimestamp // ""),
    (.reason // ""),((.involvedObject.kind // "") + "/" + (.involvedObject.name // "")),
    ((.message // "") | gsub("[\\t\\r\\n]+";" "))] | @tsv' \
  >>"$OUTPUT_DIR/warning-events.tsv" || true

node_stats=$(jq -c '{total:(.items|length),ready:([.items[] | select(
    [.status.conditions[]? | select(.type=="Ready" and .status=="True")] | length > 0)] | length),
    pressure:([.items[] | select(any(.status.conditions[]?;
      (.type=="MemoryPressure" or .type=="DiskPressure" or .type=="PIDPressure") and .status=="True"))] | length)}' \
  "$tmp_dir/nodes.json")
pod_stats=$(jq -c '{total:(.items|length),running:([.items[]|select(.status.phase=="Running")]|length),
    pending:([.items[]|select(.status.phase=="Pending")]|length),
    failed:([.items[]|select(.status.phase=="Failed")]|length),
    unready:([.items[] | select(.status.phase!="Succeeded") | select(
      .status.phase!="Running" or ((.status.containerStatuses // [])|length)==0 or
      any(.status.containerStatuses[]?;.ready!=true))] | length),
    restarts:([.items[].status.containerStatuses[]?.restartCount] | add // 0)}' "$tmp_dir/pods.json")
deployment_stats=$(jq -c '{total:(.items|length),unavailable:([.items[] | select(
    (.status.observedGeneration // 0)<(.metadata.generation // 0) or
    (.status.availableReplicas // 0)<(.spec.replicas // 0) or
    (.status.updatedReplicas // 0)<(.spec.replicas // 0))]|length)}' "$tmp_dir/deployments.json")
statefulset_stats=$(jq -c '{total:(.items|length),unavailable:([.items[] | select(
    (.status.readyReplicas // 0)<(.spec.replicas // 0))]|length)}' "$tmp_dir/statefulsets.json")
daemonset_stats=$(jq -c '{total:(.items|length),unavailable:([.items[] | select(
    (.status.numberReady // 0)<(.status.desiredNumberScheduled // 0))]|length)}' "$tmp_dir/daemonsets.json")
failed_jobs=$(jq '[.items[] | select(any(.status.conditions[]?;.type=="Failed" and .status=="True"))] | length' "$tmp_dir/jobs.json")
unbound_pvcs=$(jq '[.items[] | select(.status.phase!="Bound")] | length' "$tmp_dir/pvcs.json")
bad_apis=$(jq '[.items[] | select([.status.conditions[]? | select(.type=="Available" and .status=="True")]|length==0)]|length' "$tmp_dir/apiservices.json")
bad_certs=$(jq '[.items[] | select([.status.conditions[]? | select(.type=="Ready" and .status=="True")]|length==0)]|length' "$tmp_dir/certificates.json")
bad_routes=$(jq '[.items[] | select(
    ([.status.parents[]?.conditions[]? | select(.type=="Accepted" and .status=="True")]|length)==0 or
    ([.status.parents[]?.conditions[]? | select(.type=="ResolvedRefs" and .status=="True")]|length)==0)]|length' "$tmp_dir/httproutes.json")

jq -n \
  --arg schema "tier-live-evidence/v1" --arg collected_at "$timestamp" \
  --arg stage "$STAGE" --arg project "$project" --arg profile "$profile" \
  --arg context "$context" --arg server "$server" --argjson expected_nodes "$expected_nodes" \
  --argjson nodes "$node_stats" --argjson pods "$pod_stats" \
  --argjson deployments "$deployment_stats" --argjson statefulsets "$statefulset_stats" \
  --argjson daemonsets "$daemonset_stats" --argjson failed_jobs "$failed_jobs" \
  --argjson unbound_pvcs "$unbound_pvcs" --argjson unavailable_apis "$bad_apis" \
  --argjson unready_certificates "$bad_certs" --argjson unready_routes "$bad_routes" \
  --argjson gateway_edge "$gateway_edge" --argjson provider_edge "$provider_edge" \
  '{schema:$schema,collected_at:$collected_at,stage:$stage,project:$project,profile:$profile,
    context:$context,api_server:$server,expected_nodes:$expected_nodes,nodes:$nodes,pods:$pods,
    controllers:{deployments:$deployments,statefulsets:$statefulsets,daemonsets:$daemonsets,
      failed_jobs:$failed_jobs},storage:{unbound_pvcs:$unbound_pvcs},
    networking:{unavailable_apiservices:$unavailable_apis,unready_certificates:$unready_certificates,
      unready_httproutes:$unready_routes,gateway:$gateway_edge,provider_edge:$provider_edge},
    healthy:(($nodes.total==$expected_nodes) and ($nodes.ready==$expected_nodes) and
      ($nodes.pressure==0) and ($pods.failed==0) and ($pods.unready==0) and
      ($deployments.unavailable==0) and ($statefulsets.unavailable==0) and
      ($daemonsets.unavailable==0) and ($failed_jobs==0) and ($unbound_pvcs==0) and
      ($unavailable_apis==0) and ($unready_certificates==0) and ($unready_routes==0) and
      $gateway_edge.valid and
      (($provider_edge.checked|not) or $provider_edge.healthy))}' \
  >"$OUTPUT_DIR/evidence.json"

log "evidence collected: $OUTPUT_DIR/evidence.json"
printf '%s\n' "$OUTPUT_DIR/evidence.json"
