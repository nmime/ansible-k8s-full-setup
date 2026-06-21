#!/bin/bash
# ============================================
# Platform Orchestrator
# ============================================
# Daytona is the optional workspace platform.
# DNS: Preserves user records, only manages platform records.
# ============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/platform.yaml"
STATE_DIR="${SCRIPT_DIR}/.state"
LOG_DIR="${SCRIPT_DIR}/logs"
ANSIBLE_DIR="${SCRIPT_DIR}/.."

DEFAULT_REGION="hel1"
DEFAULT_PROJECT="k8s"

mkdir -p "${STATE_DIR}" "${LOG_DIR}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +'%H:%M:%S')]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }
error() { echo -e "${RED}[ERROR]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }

check_env() {
  if [[ -z "${HCLOUD_TOKEN:-}" ]]; then
    error "HCLOUD_TOKEN not set"
    echo "Get from: https://console.hetzner.cloud -> Security -> API Tokens"
    exit 1
  fi
}

load_config() {
  [[ ! -f "$CONFIG_FILE" ]] && { error "Run: ./platform.sh init"; exit 1; }
  PROJECT=$(yq '.global.project // "k8s"' "$CONFIG_FILE")
  TIER=$(yq '.tier // "small"' "$CONFIG_FILE")
  DOMAIN=$(yq '.global.domain' "$CONFIG_FILE")
  EMAIL=$(yq '.global.email' "$CONFIG_FILE")
  [[ -z "$DOMAIN" || "$DOMAIN" == "null" ]] && { error "global.domain is required in $CONFIG_FILE"; exit 1; }
  [[ -z "$EMAIL" || "$EMAIL" == "null" ]] && { error "global.email is required in $CONFIG_FILE"; exit 1; }
  REGION=$(yq ".infrastructure.region // \"${DEFAULT_REGION}\"" "$CONFIG_FILE")
  log "Config: project=$PROJECT, tier=$TIER, domain=$DOMAIN, region=$REGION"
}

is_enabled() {
  local path="$1"
  local value
  value=$(yq "${path} // false" "$CONFIG_FILE" 2>/dev/null) || return 1
  [[ "$value" == "true" ]]
}

flag_from_config() {
  local path="$1" default_value="${2:-false}"
  local value
  value=$(yq -r "${path} // \"${default_value}\"" "$CONFIG_FILE" 2>/dev/null || echo "$default_value")
  [[ "$value" == "true" ]] && echo true || echo false
}

run_playbook() {
  check_env
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "@${CONFIG_FILE}" \
    -e "tier=${TIER}" \
    -e "project_name=${PROJECT}" \
    -e "domain=${DOMAIN}" \
    -e "email=${EMAIL}" \
    "$@"
}

init_config() {
  local profile="${1:-example}"
  local source_file

  [[ -f "$CONFIG_FILE" ]] && { warn "platform.yaml exists"; exit 0; }

  if [[ "$profile" == "example" || "$profile" == "default" ]]; then
    source_file="${SCRIPT_DIR}/platform.example.yaml"
  else
    source_file="${SCRIPT_DIR}/profiles/${profile}.yaml"
  fi

  if [[ ! -f "$source_file" ]]; then
    error "Unknown profile: $profile"
    echo "Available profiles:"
    echo "  example"
    for file in "${SCRIPT_DIR}"/profiles/*.yaml; do
      [[ -e "$file" ]] || continue
      echo "  $(basename "$file" .yaml)"
    done
    exit 1
  fi

  cp "$source_file" "$CONFIG_FILE"
  log "Created platform.yaml from profile: $profile"
  log "Edit global.domain/global.email, then: ./platform.sh deploy all"
}

heal_check() {
  log "Health check..."
  kubectl get pods -A 2>/dev/null || { warn "cluster not accessible"; return 0; }
}

heal_auto() {
  warn "Auto-healing: deleting non-running pods"
  kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' | while read -r ns name _; do
    warn "Deleting: $ns/$name"
    kubectl delete pod "$name" -n "$ns" --grace-period=30 2>/dev/null || true
  done
}

deploy_component() {
  local component="$1"
  log "Deploying: $component"
  case "$component" in
    infra)         run_playbook --tags infrastructure 2>&1 | tee -a "${LOG_DIR}/infra.log" ;;
    network)       run_playbook --tags network 2>&1 | tee -a "${LOG_DIR}/network.log" ;;
    dns)           deploy_dns ;;
    cluster)       run_playbook --tags cluster 2>&1 | tee -a "${LOG_DIR}/cluster.log" ;;
    tls)           run_playbook --tags tls 2>&1 | tee -a "${LOG_DIR}/tls.log" ;;
    object-storage) run_playbook --tags storage,object-storage,seaweedfs 2>&1 | tee -a "${LOG_DIR}/object-storage.log" ;;
    secrets)       run_playbook --tags secrets 2>&1 | tee -a "${LOG_DIR}/secrets.log" ;;
    databases)     run_playbook --tags databases 2>&1 | tee -a "${LOG_DIR}/databases.log" ;;
    gitlab)        run_playbook --tags gitlab 2>&1 | tee -a "${LOG_DIR}/gitlab.log" ;;
    gitops)        run_playbook --tags gitops 2>&1 | tee -a "${LOG_DIR}/gitops.log" ;;
    observability) run_playbook --tags observability 2>&1 | tee -a "${LOG_DIR}/observability.log" ;;
    autoscaling)   run_playbook --tags autoscaling 2>&1 | tee -a "${LOG_DIR}/autoscaling.log" ;;
    glitchtip)     run_playbook -e deploy_glitchtip=true --tags glitchtip 2>&1 | tee -a "${LOG_DIR}/glitchtip.log" ;;
    apm)           run_playbook -e deploy_apm=true --tags apm 2>&1 | tee -a "${LOG_DIR}/apm.log" ;;
    blackbox)      run_playbook -e deploy_blackbox=true --tags blackbox 2>&1 | tee -a "${LOG_DIR}/blackbox.log" ;;
    daytona)       deploy_daytona ;;
    all)           deploy_all ;;
    *) error "Unknown component: $component"; exit 1 ;;
  esac
}

deploy_all() {
  local gt_flag apm_flag bb_flag daytona_flag
  gt_flag=$(flag_from_config '.glitchtip.enabled' false)
  apm_flag=$(flag_from_config '.apm.enabled' false)
  bb_flag=$(flag_from_config '.blackbox.enabled' true)
  daytona_flag=$(flag_from_config '.applications.daytona.enabled' false)

  run_playbook \
    -e "deploy_glitchtip=${gt_flag}" \
    -e "deploy_apm=${apm_flag}" \
    -e "deploy_blackbox=${bb_flag}" \
    -e "deploy_daytona=${daytona_flag}" \
    2>&1 | tee -a "${LOG_DIR}/deploy.log"
  log "Platform deployed!"
  show_credentials
}

deploy_daytona() {
  is_enabled '.applications.daytona.enabled' || { log "Daytona: disabled"; return 0; }
  local base_domain
  base_domain=$(yq -r ".applications.daytona.base_domain // \"daytona.${DOMAIN}\"" "$CONFIG_FILE")
  run_playbook \
    -e "deploy_daytona=true" \
    -e "daytona_base_domain=${base_domain}" \
    --tags daytona 2>&1 | tee -a "${LOG_DIR}/daytona.log"
}

generate_dns_records() {
  local ip="$1"
  local records="@ IN 3600 A ${ip}
* IN 3600 A ${ip}
vpn IN 3600 A ${ip}
api IN 3600 A ${ip}
app IN 3600 A ${ip}"
  is_enabled '.gitlab.enabled' && records+=$'\n'"gitlab IN 3600 A ${ip}"$'\n'"registry IN 3600 A ${ip}"
  is_enabled '.gitops.enabled' && records+=$'\n'"argocd IN 3600 A ${ip}"
  is_enabled '.observability.grafana.enabled' && records+=$'\n'"grafana IN 3600 A ${ip}"
  is_enabled '.storage.enabled' && records+=$'\n'"object-storage IN 3600 A ${ip}"$'\n'"s3 IN 3600 A ${ip}"
  is_enabled '.secrets.enabled' && records+=$'\n'"vault IN 3600 A ${ip}"
  is_enabled '.applications.daytona.enabled' && records+=$'\n'"daytona IN 3600 A ${ip}"$'\n'"*.daytona IN 3600 A ${ip}"
  is_enabled '.glitchtip.enabled' && records+=$'\n'"glitchtip IN 3600 A ${ip}"
  echo "$records"
}

deploy_dns() {
  log "Setting up DNS record preview (preserves existing records)..."
  check_env
  local bastion_name="${PROJECT}-bastion"
  local lb_name="${PROJECT}-lb"
  local bastion_ip lb_ip public_ip
  bastion_ip=$(hcloud server ip "$bastion_name" 2>/dev/null || echo "")
  lb_ip=$(hcloud load-balancer describe "$lb_name" -o json 2>/dev/null | jq -r '.public_net.ipv4.ip' || echo "")
  public_ip="${lb_ip:-$bastion_ip}"
  [[ -z "$public_ip" ]] && { error "No target IP found"; exit 1; }
  generate_dns_records "$public_ip"
}

destroy_all() {
  warn "DESTROY project '$PROJECT'?"
  read -rp "Type 'DESTROY': " confirm
  [[ "$confirm" != "DESTROY" ]] && exit 0
  run_playbook -e state=absent 2>&1 | tee -a "${LOG_DIR}/destroy.log"
  rm -f ~/.kube/config
  rm -rf "${STATE_DIR:?}"/*
  log "Destroyed! DNS zone preserved."
}

show_status() {
  echo "Platform: ${PROJECT:-k8s} / ${DOMAIN:-unknown} (${TIER:-unknown}) [${REGION:-unknown}]"
  echo "=========================================="
  hcloud server list 2>/dev/null | grep "${PROJECT:-k8s}" || echo "(no servers)"
  echo ""
  kubectl get nodes 2>/dev/null || echo "(cluster not accessible)"
}

show_credentials() {
  echo "Credentials for $DOMAIN"
  echo "=========================================="
  kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' &>/dev/null && {
    echo "GitLab: https://gitlab.$DOMAIN"
    echo "  User: root"
    echo "  Pass: $(kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' | base64 -d)"
  }
  kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' &>/dev/null && {
    echo "ArgoCD: https://argocd.$DOMAIN"
    echo "  User: admin"
    echo "  Pass: $(kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d)"
  }
  echo "Vault: https://vault.$DOMAIN"
  echo "PostgreSQL: kubectl get secret ${PROJECT:-k8s}-pg-pguser-app -n databases -o jsonpath='{.data.password}' | base64 -d"
  echo "Dragonfly: kubectl get secret dragonfly-auth -n dragonfly -o jsonpath='{.data.password}' | base64 -d"
  if is_enabled '.applications.daytona.enabled'; then
    echo "Daytona: https://$(yq -r ".applications.daytona.base_domain // \"daytona.${DOMAIN}\"" "$CONFIG_FILE")"
  fi
}

show_help() {
  cat << 'EOF'
Platform Orchestrator
=====================
DNS: Preserves user records, only manages platform records.

Usage: ./platform.sh <command>

Commands:
  init [profile]    Create config from platform.example.yaml or profiles/<profile>.yaml
  deploy all        Full deployment
  deploy <comp>     infra|network|dns|cluster|tls|object-storage|secrets|databases|gitlab|gitops|observability|autoscaling|daytona|glitchtip|apm|blackbox
  status            Show status
  credentials       Show passwords
  health / heal     Check/fix
  destroy           Remove all (DNS: only platform records removed)

Required:
  export HCLOUD_TOKEN="your-token"
EOF
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    deploy)       load_config; deploy_component "${1:-all}" ;;
    destroy)      load_config; destroy_all ;;
    status)       load_config 2>/dev/null || true; show_status ;;
    credentials)  load_config; show_credentials ;;
    health)       heal_check ;;
    heal)         heal_check; heal_auto ;;
    init)         init_config "${1:-example}" ;;
    *)            show_help ;;
  esac
}

main "$@"
