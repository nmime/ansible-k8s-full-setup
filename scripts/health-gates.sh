#!/bin/bash
# ============================================
# Health Gate Checks
# ============================================
# Source this script, then call check_health_gates.
# ============================================

HEALTH_GATE_FAILURES=0
HEALTH_DRY_RUN=false
HEALTH_REQUIRE_ARGOCD="${HEALTH_REQUIRE_ARGOCD:-true}"
HEALTH_REQUIRE_POSTGRESQL="${HEALTH_REQUIRE_POSTGRESQL:-true}"
HEALTH_REQUIRE_MONGODB="${HEALTH_REQUIRE_MONGODB:-false}"
HEALTH_EXPECTED_NODES="${HEALTH_EXPECTED_NODES:-}"
HEALTH_CONFIG_FILE="${HEALTH_CONFIG_FILE:-}"

_hg_log()    { echo -e "\033[0;32m[$(date +'%H:%M:%S')] $1"; }
_hg_warn()   { echo -e "\033[1;33m[WARN] $1"; }
_hg_error()  { echo -e "\033[0;31m[ERROR] $1"; }

_hg_parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) HEALTH_DRY_RUN=true; shift ;;
      --config)
        [[ $# -ge 2 ]] || { _hg_error "--config requires a file path or 'active'"; return 2; }
        HEALTH_CONFIG_FILE="$2"
        shift 2
        ;;
      -h|--help)
        echo "Usage: health-gates.sh [--config FILE|active] [--dry-run]"
        return 3
        ;;
      *) _hg_error "Unknown argument: $1"; return 2 ;;
    esac
  done

  if [[ -n "$HEALTH_CONFIG_FILE" ]]; then
    if [[ "$HEALTH_CONFIG_FILE" == "active" ]]; then
      HEALTH_CONFIG_FILE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/platform-orchestrator/platform.yaml
    fi
    [[ -f "$HEALTH_CONFIG_FILE" ]] || { _hg_error "Config not found: $HEALTH_CONFIG_FILE"; return 2; }
    command -v yq >/dev/null 2>&1 || { _hg_error "yq is required with --config"; return 2; }
    HEALTH_REQUIRE_ARGOCD=$(yq -r '.gitops.enabled // false' "$HEALTH_CONFIG_FILE")
    HEALTH_REQUIRE_POSTGRESQL=$(yq -r '(.databases.enabled and .databases.postgresql.enabled) // false' "$HEALTH_CONFIG_FILE")
    HEALTH_REQUIRE_MONGODB=$(yq -r '(.databases.enabled and .databases.mongodb.enabled) // false' "$HEALTH_CONFIG_FILE")
    HEALTH_EXPECTED_NODES=$(yq -r '(.infrastructure.control_plane.count // 0) + (.infrastructure.workers.count // 0)' "$HEALTH_CONFIG_FILE")
  fi
}

_hg_wait_for_api() {
  local attempt
  for attempt in {1..6}; do
    if kubectl get --raw=/readyz >/dev/null 2>&1; then
      return 0
    fi
    if [[ "$attempt" -lt 6 ]]; then
      _hg_warn "Kubernetes API is temporarily unavailable (attempt $attempt/6); retrying"
      sleep 5
    fi
  done
  _hg_error "Kubernetes API did not become ready after 6 attempts"
  return 1
}

_hg_check_nodes() {
  _hg_log "Health gate: Nodes"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check kubectl get nodes"; return 0; fi
  if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "  Cluster is unreachable"; ((HEALTH_GATE_FAILURES++)) || true; return
  fi
  local not_ready
  not_ready=$(kubectl get nodes -o json 2>/dev/null | jq '[
    .items[]
    | select([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 0)
  ] | length')
  if [[ "$not_ready" -eq 0 ]]; then
    echo "  All nodes Ready"
  else
    echo "  $not_ready node(s) not Ready"; ((HEALTH_GATE_FAILURES++)) || true
  fi
  if [[ -n "$HEALTH_EXPECTED_NODES" ]]; then
    local actual_nodes
    actual_nodes=$(kubectl get nodes -o json | jq '.items | length')
    if [[ "$actual_nodes" -eq "$HEALTH_EXPECTED_NODES" ]]; then
      echo "  Node count: $actual_nodes/$HEALTH_EXPECTED_NODES"
    else
      echo "  Node count: $actual_nodes/$HEALTH_EXPECTED_NODES"; ((HEALTH_GATE_FAILURES++)) || true
    fi
  fi
}

_hg_check_cilium() {
  _hg_log "Health gate: Cilium"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check Cilium pods"; return 0; fi
  local pods total running
  pods=$(kubectl get pods -n kube-system -l k8s-app=cilium -o json 2>/dev/null || printf '{"items":[]}')
  total=$(jq '.items | length' <<<"$pods")
  running=$(jq '[.items[] | select(.status.phase == "Running" and
    ((.status.containerStatuses // []) | length > 0) and
    all(.status.containerStatuses[]; .ready == true))] | length' <<<"$pods")
  if [[ "$total" -gt 0 ]]; then
    echo "  Cilium: $running/$total Running"
    if [[ "$running" -ne "$total" ]]; then
      ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  Cilium pods not found"; ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_cert_manager() {
  _hg_log "Health gate: Cert-manager"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check cert-manager pods"; return 0; fi
  local pods total running
  pods=$(kubectl get pods -n cert-manager -o json 2>/dev/null || printf '{"items":[]}')
  # ReplicaSet replacement pods can remain in Succeeded after a node drain.
  # They are terminal history, not desired cert-manager capacity. Failed terminal
  # pods remain in the denominator so a real crash still fails the gate.
  total=$(jq '[.items[]
    | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
    | select(.status.phase != "Succeeded")] | length' <<<"$pods")
  running=$(jq '[.items[]
    | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
    | select(.status.phase != "Succeeded")
    | select(.status.phase == "Running" and
      ((.status.containerStatuses // []) | length > 0) and
      all(.status.containerStatuses[]; .ready == true))] | length' <<<"$pods")
  if [[ "$total" -gt 0 ]]; then
    echo "  Cert-manager: $running/$total Running"
    if [[ "$running" -ne "$total" ]]; then
      ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  Cert-manager not deployed"; ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_aggregated_apis() {
  _hg_log "Health gate: Aggregated API services"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check APIService availability"; return 0; fi
  local services total unavailable unavailable_names
  services=$(kubectl get apiservices.apiregistration.k8s.io -o json 2>/dev/null || printf '{"items":[]}')
  total=$(jq '.items | length' <<<"$services")
  unavailable=$(jq '[.items[]
    | select([.status.conditions[]? | select(.type == "Available" and .status == "True")] | length == 0)]
    | length' <<<"$services")
  echo "  API services: $((total - unavailable))/$total Available"
  if [[ "$unavailable" -ne 0 ]]; then
    unavailable_names=$(jq -r '.items[]
      | select([.status.conditions[]? | select(.type == "Available" and .status == "True")] | length == 0)
      | .metadata.name' <<<"$services" | paste -sd, -)
    echo "  Unavailable API services: $unavailable_names"
    ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_argocd() {
  _hg_log "Health gate: ArgoCD"
  if [[ "$HEALTH_REQUIRE_ARGOCD" != "true" ]]; then echo "  ArgoCD disabled by profile"; return 0; fi
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check ArgoCD pods"; return 0; fi
  local pods total running
  pods=$(kubectl get pods -n argocd -o json 2>/dev/null || printf '{"items":[]}')
  # ReplicaSet replacement pods can remain in Succeeded after a node drain.
  # They are terminal history, not desired Argo CD capacity. Failed terminal
  # pods remain in the denominator so a real crash still fails the gate.
  total=$(jq '[.items[]
    | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
    | select(.status.phase != "Succeeded")] | length' <<<"$pods")
  running=$(jq '[.items[]
    | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
    | select(.status.phase != "Succeeded")
    | select(.status.phase == "Running" and
      ((.status.containerStatuses // []) | length > 0) and
      all(.status.containerStatuses[]; .ready == true))] | length' <<<"$pods")
  if [[ "$total" -gt 0 ]]; then
    echo "  ArgoCD: $running/$total Running"
    if [[ "$running" -ne "$total" ]]; then
      ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  ArgoCD not deployed"; ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_databases() {
  _hg_log "Health gate: Databases"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check database pods"; return 0; fi
  local pods pg_total pg_running mg_total mg_running
  pods=$(kubectl get pods -n databases -o json 2>/dev/null || printf '{"items":[]}')
  if [[ "$HEALTH_REQUIRE_POSTGRESQL" == "true" ]]; then
    pg_total=$(jq '[.items[]
      | select(.metadata.name | test("postgres|pg-|pgbouncer"; "i"))
      | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)] | length' <<<"$pods")
    pg_running=$(jq '[.items[]
      | select(.metadata.name | test("postgres|pg-|pgbouncer"; "i"))
      | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
      | select(.status.phase == "Running" and
        ((.status.containerStatuses // []) | length > 0) and
        all(.status.containerStatuses[]; .ready == true))] | length' <<<"$pods")
    if [[ "$pg_total" -gt 0 && "$pg_running" -eq "$pg_total" ]]; then
      echo "  PostgreSQL: $pg_running/$pg_total Running"
    else
      echo "  PostgreSQL: $pg_running/$pg_total Running"; ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  PostgreSQL disabled by profile"
  fi
  if [[ "$HEALTH_REQUIRE_MONGODB" == "true" ]]; then
    mg_total=$(jq '[.items[]
      | select(.metadata.name | test("mongo"; "i"))
      | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)] | length' <<<"$pods")
    mg_running=$(jq '[.items[]
      | select(.metadata.name | test("mongo"; "i"))
      | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
      | select(.status.phase == "Running" and
        ((.status.containerStatuses // []) | length > 0) and
        all(.status.containerStatuses[]; .ready == true))] | length' <<<"$pods")
    if [[ "$mg_total" -gt 0 && "$mg_running" -eq "$mg_total" ]]; then
      echo "  MongoDB: $mg_running/$mg_total Running"
    else
      echo "  MongoDB: $mg_running/$mg_total Running"; ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  MongoDB disabled by profile"
  fi
}

_hg_check_workload_controllers() {
  _hg_log "Health gate: All workload controllers and pods"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check pods, Deployments, StatefulSets, DaemonSets, and Jobs"; return 0; fi
  local bad_pods bad_deployments bad_statefulsets bad_daemonsets failed_jobs
  bad_pods=$(kubectl get pods -A -o json | jq '[.items[]
    | select([.metadata.ownerReferences[]? | select(.kind == "Job")] | length == 0)
    | select(.status.phase != "Succeeded")
    | select(.status.phase != "Running" or
      ((.status.containerStatuses // []) | length == 0) or
      any(.status.containerStatuses[]; .ready != true))] | length')
  bad_deployments=$(kubectl get deployments -A -o json | jq '[.items[]
    | select((.status.observedGeneration // 0) < (.metadata.generation // 0) or
      (.status.availableReplicas // 0) < (.spec.replicas // 0) or
      (.status.updatedReplicas // 0) < (.spec.replicas // 0))] | length')
  bad_statefulsets=$(kubectl get statefulsets -A -o json | jq '[.items[]
    | select((.status.observedGeneration // 0) < (.metadata.generation // 0) or
      (.status.readyReplicas // 0) < (.spec.replicas // 0) or
      ((.spec.updateStrategy.type // "RollingUpdate") == "RollingUpdate" and
       (.status.updatedReplicas // 0) < (.spec.replicas // 0)))] | length')
  bad_daemonsets=$(kubectl get daemonsets -A -o json | jq '[.items[]
    | select((.status.observedGeneration // 0) < (.metadata.generation // 0) or
      (.status.numberReady // 0) < (.status.desiredNumberScheduled // 0) or
      (.status.updatedNumberScheduled // 0) < (.status.desiredNumberScheduled // 0))] | length')
  failed_jobs=$(kubectl get jobs -A -o json | jq '[.items[]
    | select(any(.status.conditions[]?; .type == "Failed" and .status == "True"))] | length')
  echo "  Unhealthy: pods=$bad_pods deployments=$bad_deployments statefulsets=$bad_statefulsets daemonsets=$bad_daemonsets failed-jobs=$failed_jobs"
  if (( bad_pods + bad_deployments + bad_statefulsets + bad_daemonsets + failed_jobs > 0 )); then
    ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_storage_and_routes() {
  _hg_log "Health gate: Persistent storage, certificates, and Gateway routes"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check PVCs, Certificates, and HTTPRoutes"; return 0; fi
  local bad_pvcs=0 bad_certificates=0 bad_routes=0
  bad_pvcs=$(kubectl get pvc -A -o json | jq '[.items[] | select(.status.phase != "Bound")] | length')
  if kubectl api-resources --api-group=cert-manager.io -o name | grep -qx 'certificates.cert-manager.io'; then
    bad_certificates=$(kubectl get certificates -A -o json | jq '[.items[]
      | select([.status.conditions[]? | select(.type == "Ready" and .status == "True")] | length == 0)] | length')
  fi
  if kubectl api-resources --api-group=gateway.networking.k8s.io -o name | grep -qx 'httproutes.gateway.networking.k8s.io'; then
    bad_routes=$(kubectl get httproutes -A -o json | jq '[.items[]
      | select(( [.status.parents[]?.conditions[]? | select(.type == "Accepted" and .status == "True")] | length) == 0 or
        ( [.status.parents[]?.conditions[]? | select(.type == "ResolvedRefs" and .status == "True")] | length) == 0)] | length')
  fi
  echo "  Unhealthy: pvc=$bad_pvcs certificates=$bad_certificates httproutes=$bad_routes"
  if (( bad_pvcs + bad_certificates + bad_routes > 0 )); then
    ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_security_baseline() {
  _hg_log "Health gate: Runtime security baseline"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check anonymous RBAC, privileged application pods, and Cilium policy validity"; return 0; fi
  local anonymous_can_create privileged_application_containers invalid_cilium_policies=0
  # `kubectl auth can-i --as=system:anonymous` asks the impersonated identity
  # to create a SelfSubjectAccessReview, which hardened clusters correctly
  # forbid and therefore reports an ambiguous client error. A server-side dry
  # run exercises the actual permission without persisting a Secret.
  if kubectl create secret generic health-gate-anonymous-probe \
    --namespace default \
    --from-literal=probe=denied \
    --dry-run=server \
    --output=name \
    --as=system:anonymous >/dev/null 2>&1; then
    anonymous_can_create=yes
  else
    anonymous_can_create=no
  fi
  privileged_application_containers=$(kubectl get pods -A -o json | jq '[.items[]
    | select(.metadata.namespace != "kube-system" and
      .metadata.namespace != "cilium-system" and
      .metadata.namespace != "metallb-system" and
      .metadata.namespace != "coroot")
    | ((.spec.initContainers // []) + (.spec.containers // []))[]
    | select(.securityContext.privileged == true)] | length')
  if kubectl api-resources --api-group=cilium.io -o name \
    | grep -qx 'ciliumnetworkpolicies.cilium.io'; then
    invalid_cilium_policies=$(kubectl get ciliumnetworkpolicies -A -o json | jq '[.items[]
      | select(([.status.conditions[]?
        | select(.type == "Valid" and .status == "True")] | length) == 0)] | length')
  fi
  echo "  Anonymous create secrets: $anonymous_can_create"
  echo "  Privileged application containers: $privileged_application_containers"
  echo "  Invalid CiliumNetworkPolicies: $invalid_cilium_policies"
  if [[ "$anonymous_can_create" != "no" ]] \
    || [[ "$privileged_application_containers" -ne 0 ]] \
    || [[ "$invalid_cilium_policies" -ne 0 ]]; then
    ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_helm_releases() {
  _hg_log "Health gate: Helm releases"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check failed Helm releases"; return 0; fi
  if ! command -v helm >/dev/null 2>&1; then
    echo "  helm not installed"; ((HEALTH_GATE_FAILURES++)) || true; return
  fi
  local failed
  failed=$(helm list --all-namespaces --failed --short 2>/dev/null | sed '/^[[:space:]]*$/d' | wc -l | tr -d ' ')
  echo "  Failed Helm releases: $failed"
  if [[ "$failed" -ne 0 ]]; then ((HEALTH_GATE_FAILURES++)) || true; fi
}

check_health_gates() {
  local parse_rc=0
  _hg_parse_args "$@" || parse_rc=$?
  [[ "$parse_rc" -eq 0 ]] || { [[ "$parse_rc" -eq 3 ]] && return 0; return "$parse_rc"; }
  HEALTH_GATE_FAILURES=0
  echo ""
  _hg_log "=== HEALTH GATE CHECKS ==="
  if [[ "$HEALTH_DRY_RUN" != true ]] && ! command -v jq >/dev/null 2>&1; then
    _hg_error "jq is required for exact readiness checks"
    return 1
  fi
  if [[ "$HEALTH_DRY_RUN" != true ]] && ! _hg_wait_for_api; then
    return 1
  fi
  _hg_check_nodes
  _hg_check_cilium
  _hg_check_cert_manager
  _hg_check_aggregated_apis
  _hg_check_argocd
  _hg_check_databases
  _hg_check_workload_controllers
  _hg_check_storage_and_routes
  _hg_check_security_baseline
  _hg_check_helm_releases
  if $HEALTH_DRY_RUN; then
    echo "  [DRY-RUN] Would evaluate all gates"
    return 0
  fi
  echo ""
  if [[ "$HEALTH_GATE_FAILURES" -eq 0 ]]; then
    _hg_log "All health gates passed"
    return 0
  else
    _hg_error "$HEALTH_GATE_FAILURES health gate(s) failed"
    return 1
  fi
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  check_health_gates "$@"
fi
