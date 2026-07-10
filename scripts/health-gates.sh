#!/bin/bash
# ============================================
# Health Gate Checks
# ============================================
# Source this script, then call check_health_gates.
# ============================================

HEALTH_GATE_FAILURES=0
HEALTH_DRY_RUN=false

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
    [[ "$running" -eq "$total" ]] && echo "  Cilium: $running/$total Running" || \
      { echo "  Cilium: $running/$total Running"; ((HEALTH_GATE_FAILURES++)) || true; }
  else
    echo "  Cilium pods not found"
  fi
}

_hg_check_cert_manager() {
  _hg_log "Health gate: Cert-manager"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check cert-manager pods"; return 0; fi
  local total running
  total=$(kubectl get pods -n cert-manager --no-headers 2>/dev/null | grep -c 'cert-manager' || echo 0)
  running=$(kubectl get pods -n cert-manager --no-headers 2>/dev/null | grep 'cert-manager' | grep -c 'Running' || echo 0)
  if [[ "$total" -gt 0 ]]; then
    [[ "$running" -eq "$total" ]] && echo "  Cert-manager: $running/$total Running" || \
      { echo "  Cert-manager: $running/$total Running"; ((HEALTH_GATE_FAILURES++)) || true; }
  else
    echo "  Cert-manager not deployed"
  fi
}

_hg_check_argocd() {
  _hg_log "Health gate: ArgoCD"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check ArgoCD pods"; return 0; fi
  local total running
  total=$(kubectl get pods -n argocd --no-headers 2>/dev/null | wc -l || echo 0)
  running=$(kubectl get pods -n argocd --no-headers 2>/dev/null | grep -c 'Running' || echo 0)
  if [[ "$total" -gt 0 ]]; then
    [[ "$running" -eq "$total" ]] && echo "  ArgoCD: $running/$total Running" || \
      { echo "  ArgoCD: $running/$total Running"; ((HEALTH_GATE_FAILURES++)) || true; }
  else
    echo "  ArgoCD not deployed"
  fi
}

_hg_check_databases() {
  _hg_log "Health gate: Databases"
  if $HEALTH_DRY_RUN; then echo "  [DRY-RUN] Would check database pods"; return 0; fi
  for db_ns in postgres k8s-databases database; do
    local pg_total
    pg_total=$(kubectl get pods -n "$db_ns" --no-headers 2>/dev/null | grep -ci 'postgres' || echo 0)
    if [[ "$pg_total" -gt 0 ]]; then
      local pg_running
      pg_running=$(kubectl get pods -n "$db_ns" --no-headers 2>/dev/null | grep -i 'postgres' | grep -c 'Running' || echo 0)
      [[ "$pg_running" -eq "$pg_total" ]] && echo "  PostgreSQL ($db_ns): $pg_running/$pg_total Running" || \
        { echo "  PostgreSQL ($db_ns): $pg_running/$pg_total Running"; ((HEALTH_GATE_FAILURES++)) || true; }
      return
    fi
  done
  echo "  No PostgreSQL pods found"
  for mg_ns in postgres k8s-databases database; do
    local mg_total
    mg_total=$(kubectl get pods -n "$mg_ns" --no-headers 2>/dev/null | grep -ci 'mongo' || echo 0)
    if [[ "$mg_total" -gt 0 ]]; then
      local mg_running
      mg_running=$(kubectl get pods -n "$mg_ns" --no-headers 2>/dev/null | grep -i 'mongo' | grep -c 'Running' || echo 0)
      [[ "$mg_running" -eq "$mg_total" ]] && echo "  MongoDB ($mg_ns): $mg_running/$mg_total Running" || \
        { echo "  MongoDB ($mg_ns): $mg_running/$mg_total Running"; ((HEALTH_GATE_FAILURES++)) || true; }
      return
    fi
  done
  echo "  No MongoDB pods found"
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
