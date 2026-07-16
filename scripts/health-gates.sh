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

_hg_log()    { echo -e "\033[0;32m[$(date +'%H:%M:%S')] $1"; }
_hg_warn()   { echo -e "\033[1;33m[WARN] $1"; }
_hg_error()  { echo -e "\033[0;31m[ERROR] $1"; }

_hg_parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) HEALTH_DRY_RUN=true; shift ;;
      *) shift ;;
    esac
  done
}

_hg_check_nodes() {
  _hg_log "Health gate: Nodes"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check kubectl get nodes"; return 0; fi
  if ! kubectl cluster-info >/dev/null 2>&1; then
    echo "  Cluster is unreachable"; ((HEALTH_GATE_FAILURES++)) || true; return
  fi
  local not_ready
  not_ready=$(kubectl get nodes --no-headers 2>/dev/null | grep -cv 'Ready' || echo 0)
  if [[ "$not_ready" -eq 0 ]]; then
    echo "  All nodes Ready"
  else
    echo "  $not_ready node(s) not Ready"; ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_cilium() {
  _hg_log "Health gate: Cilium"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check Cilium pods"; return 0; fi
  local total running
  total=$(kubectl get pods -n kube-system -l k8s-app=cilium --no-headers 2>/dev/null | wc -l || echo 0)
  running=$(kubectl get pods -n kube-system -l k8s-app=cilium --no-headers 2>/dev/null | grep -c 'Running' || echo 0)
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
  local total running
  total=$(kubectl get pods -n cert-manager --no-headers 2>/dev/null | grep -c 'cert-manager' || echo 0)
  running=$(kubectl get pods -n cert-manager --no-headers 2>/dev/null | grep 'cert-manager' | grep -c 'Running' || echo 0)
  if [[ "$total" -gt 0 ]]; then
    echo "  Cert-manager: $running/$total Running"
    if [[ "$running" -ne "$total" ]]; then
      ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  Cert-manager not deployed"; ((HEALTH_GATE_FAILURES++)) || true
  fi
}

_hg_check_argocd() {
  _hg_log "Health gate: ArgoCD"
  if [[ "$HEALTH_REQUIRE_ARGOCD" != "true" ]]; then echo "  ArgoCD disabled by profile"; return 0; fi
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check ArgoCD pods"; return 0; fi
  local total running
  total=$(kubectl get pods -n argocd --no-headers 2>/dev/null | wc -l || echo 0)
  running=$(kubectl get pods -n argocd --no-headers 2>/dev/null | grep -c 'Running' || echo 0)
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
  local pg_total pg_running mg_total mg_running
  if [[ "$HEALTH_REQUIRE_POSTGRESQL" == "true" ]]; then
    pg_total=$(kubectl get pods -n databases --no-headers 2>/dev/null | grep -Eci 'postgres|pg-' || echo 0)
    pg_running=$(kubectl get pods -n databases --no-headers 2>/dev/null | grep -Ei 'postgres|pg-' | grep -c 'Running' || echo 0)
    if [[ "$pg_total" -gt 0 && "$pg_running" -eq "$pg_total" ]]; then
      echo "  PostgreSQL: $pg_running/$pg_total Running"
    else
      echo "  PostgreSQL: $pg_running/$pg_total Running"; ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  PostgreSQL disabled by profile"
  fi
  if [[ "$HEALTH_REQUIRE_MONGODB" == "true" ]]; then
    mg_total=$(kubectl get pods -n databases --no-headers 2>/dev/null | grep -ci 'mongo' || echo 0)
    mg_running=$(kubectl get pods -n databases --no-headers 2>/dev/null | grep -i 'mongo' | grep -c 'Running' || echo 0)
    if [[ "$mg_total" -gt 0 && "$mg_running" -eq "$mg_total" ]]; then
      echo "  MongoDB: $mg_running/$mg_total Running"
    else
      echo "  MongoDB: $mg_running/$mg_total Running"; ((HEALTH_GATE_FAILURES++)) || true
    fi
  else
    echo "  MongoDB disabled by profile"
  fi
}

check_health_gates() {
  _hg_parse_args "$@"
  HEALTH_GATE_FAILURES=0
  echo ""
  _hg_log "=== HEALTH GATE CHECKS ==="
  _hg_check_nodes
  _hg_check_cilium
  _hg_check_cert_manager
  _hg_check_argocd
  _hg_check_databases
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
