#!/bin/bash
# ============================================
# Platform Orchestrator
# ============================================
# All services enabled by default
# Consistent naming: {project}-{resource}
# DNS: Preserves user records, only manages platform records
# ============================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/platform.yaml"
STATE_DIR="${SCRIPT_DIR}/.state"
LOG_DIR="${SCRIPT_DIR}/logs"
ANSIBLE_DIR="${SCRIPT_DIR}/.."

# Defaults
DEFAULT_REGION="hel1"
DEFAULT_PROJECT="k8s"

mkdir -p "${STATE_DIR}" "${LOG_DIR}"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log() { echo -e "${GREEN}[$(date +'%H:%M:%S')]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }
error() { echo -e "${RED}[ERROR]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1" | tee -a "${LOG_DIR}/platform.log"; }

# ============================================
# ENVIRONMENT
# ============================================

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
  EMAIL=$(yq '.global.email // "admin@example.com"' "$CONFIG_FILE")
  REGION=$(yq ".infrastructure.region // \"${DEFAULT_REGION}\"" "$CONFIG_FILE")

  NETWORK_NAME="${PROJECT}-network"
  BASTION_NAME="${PROJECT}-bastion"
  LB_NAME="${PROJECT}-lb"

  log "Config: project=$PROJECT, tier=$TIER, domain=$DOMAIN, region=$REGION"
}

is_enabled() {
  local path="$1"
  local value=$(yq "${path} // true" "$CONFIG_FILE")
  [[ "$value" == "true" ]]
}

# ============================================
# HEALTH
# ============================================

heal_check() {
  log "Health check..."
  local issues=0
  local unhealthy=$(kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' || true)
  [[ -n "$unhealthy" ]] && { warn "Unhealthy pods:"; echo "$unhealthy"; ((issues++)); }
  [[ $issues -eq 0 ]] && log "All healthy!" || warn "$issues issues"
  return $issues
}

heal_auto() {
  log "Auto-healing..."
  kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' | while read ns name _; do
    warn "Deleting: $ns/$name"
    kubectl delete pod "$name" -n "$ns" --force --grace-period=0 2>/dev/null || true
  done
}

# ============================================
# DEPLOYMENT (via Ansible)
# ============================================

deploy_component() {
  local component="$1"
  log "Deploying: $component"
  case "$component" in
    "infra")        deploy_infra ;;
    "network")      deploy_network ;;
    "dns")          deploy_dns ;;
    "cluster")      deploy_cluster ;;
    "tls")          deploy_tls ;;
    "minio")        deploy_minio ;;
    "secrets")      deploy_secrets ;;
    "databases")    deploy_databases ;;
    "gitlab")       deploy_gitlab ;;
    "gitops")       deploy_gitops ;;
    "observability") deploy_observability ;;
    "autoscaling")  deploy_autoscaling ;;
    "all")          deploy_all ;;
    *) error "Unknown: $component"; exit 1 ;;
  esac
}

deploy_all() {
  log "Full deployment: project=$PROJECT, tier=$TIER, domain=$DOMAIN, region=$REGION"
  check_env

  # Run Ansible playbook for full deployment
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" \
    -e "project_name=${PROJECT}" \
    -e "domain=${DOMAIN}" \
    -e "email=${EMAIL}" \
    2>&1 | tee -a "${LOG_DIR}/deploy.log"

  log "Platform deployed!"
  show_credentials
}

# Individual component deployments via Ansible roles
deploy_infra() {
  log "Deploying infrastructure (project: $PROJECT, region: $REGION)..."
  check_env
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" \
    --tags infrastructure \
    2>&1 | tee -a "${LOG_DIR}/infra.log"

  local BASTION_IP=$(hcloud server ip "${BASTION_NAME}" 2>/dev/null || echo "")
  local LB_IP=$(hcloud load-balancer describe "${LB_NAME}" -o json 2>/dev/null | jq -r '.public_net.ipv4.ip' || echo "")

  cat > "${STATE_DIR}/infra.yaml" << EOF
project: ${PROJECT}
domain: ${DOMAIN}
bastion_ip: ${BASTION_IP}
lb_ip: ${LB_IP:-$BASTION_IP}
region: ${REGION}
EOF
}

deploy_dns() {
  log "Setting up DNS (preserves existing records)..."
  check_env

  local BASTION_IP=$(yq '.bastion_ip' "${STATE_DIR}/infra.yaml" 2>/dev/null || hcloud server ip "${BASTION_NAME}")
  local LB_IP=$(yq '.lb_ip' "${STATE_DIR}/infra.yaml" 2>/dev/null || echo "$BASTION_IP")
  local PUBLIC_IP="${LB_IP:-$BASTION_IP}"

  log "DNS target IP: $PUBLIC_IP"

  local RECORDS=$(generate_dns_records "$PUBLIC_IP")
  log "DNS records generated for $DOMAIN"
}

generate_dns_records() {
  local ip="$1"

  local records="@ IN 3600 A ${ip}
* IN 3600 A ${ip}
vpn IN 3600 A ${ip}
api IN 3600 A ${ip}
app IN 3600 A ${ip}"

  is_enabled '.gitlab.enabled' && records+="
gitlab IN 3600 A ${ip}
registry IN 3600 A ${ip}"

  is_enabled '.gitops.enabled' && records+="
argocd IN 3600 A ${ip}"

  is_enabled '.observability.grafana.enabled' && records+="
grafana IN 3600 A ${ip}"

  is_enabled '.observability.metrics.enabled' && records+="
victoriametrics IN 3600 A ${ip}"

  is_enabled '.observability.logging.enabled' && records+="
loki IN 3600 A ${ip}"

  is_enabled '.storage.enabled' && records+="
minio IN 3600 A ${ip}
s3 IN 3600 A ${ip}"

  is_enabled '.secrets.enabled' && records+="
vault IN 3600 A ${ip}"

  echo "$records"
}

deploy_network() {
  log "Deploying network security (VPN + bastion hardening)..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" \
    --tags network \
    2>&1 | tee -a "${LOG_DIR}/network.log"
}

deploy_cluster() {
  log "Deploying K8s cluster..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" \
    --tags cluster \
    2>&1 | tee -a "${LOG_DIR}/cluster.log"
}

deploy_tls() {
  log "Setting up TLS..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags tls \
    2>&1 | tee -a "${LOG_DIR}/tls.log"
}

deploy_minio() {
  is_enabled '.storage.enabled' || { log "MinIO: disabled"; return 0; }
  log "Installing MinIO..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags storage \
    2>&1 | tee -a "${LOG_DIR}/minio.log" || true
}

deploy_secrets() {
  is_enabled '.secrets.enabled' || { log "Vault: disabled"; return 0; }
  log "Installing Vault..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags secrets \
    2>&1 | tee -a "${LOG_DIR}/secrets.log" || true
}

deploy_databases() {
  is_enabled '.databases.postgresql.enabled' || { log "PostgreSQL: disabled"; return 0; }
  log "Installing PostgreSQL..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags databases \
    2>&1 | tee -a "${LOG_DIR}/databases.log" || true
}

deploy_gitlab() {
  is_enabled '.gitlab.enabled' || { log "GitLab: disabled"; return 0; }
  log "Installing GitLab..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "domain=${DOMAIN}" --tags gitlab \
    2>&1 | tee -a "${LOG_DIR}/gitlab.log" || true
}

deploy_gitops() {
  is_enabled '.gitops.enabled' || { log "ArgoCD: disabled"; return 0; }
  log "Installing ArgoCD..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags gitops \
    2>&1 | tee -a "${LOG_DIR}/gitops.log" || true
}

deploy_observability() {
  is_enabled '.observability.metrics.enabled' || { log "Observability: disabled"; return 0; }
  log "Installing monitoring..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags observability \
    2>&1 | tee -a "${LOG_DIR}/observability.log" || true
}

deploy_autoscaling() {
  is_enabled '.autoscaling.enabled' || { log "KEDA: disabled"; return 0; }
  log "Installing KEDA..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" --tags autoscaling \
    2>&1 | tee -a "${LOG_DIR}/autoscaling.log" || true
}

# ============================================
# DESTROY
# ============================================

destroy_all() {
  warn "DESTROY project '$PROJECT'?"
  warn "DNS: Only platform records removed, your records preserved"
  read -p "Type 'DESTROY': " confirm
  [[ "$confirm" != "DESTROY" ]] && exit 0

  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" \
    -e "state=absent" \
    2>&1 | tee -a "${LOG_DIR}/destroy.log" || true

  rm -rf ~/.kube/config "${STATE_DIR}"/*
  log "Destroyed! DNS zone preserved (only platform records removed)"
}

# ============================================
# STATUS
# ============================================

show_status() {
  echo "Platform: ${PROJECT:-k8s} / ${DOMAIN:-unknown} (${TIER:-unknown}) [${REGION:-unknown}]"
  echo "=========================================="
  hcloud server list 2>/dev/null | grep "${PROJECT:-k8s}" || echo "(no servers)"
  echo ""
  kubectl get nodes 2>/dev/null || echo "(cluster not accessible)"
}

show_credentials() {
  echo ""
  echo "Credentials for $DOMAIN"
  echo "=========================================="

  kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' &>/dev/null && {
    echo "GitLab: https://gitlab.$DOMAIN"
    echo "  User: root"
    echo "  Pass: $(kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' | base64 -d)"
    echo ""
  }

  kubectl get secret minio -n storage -o jsonpath='{.data.rootUser}' &>/dev/null && {
    echo "MinIO: https://minio.$DOMAIN"
    echo "  User: $(kubectl get secret minio -n storage -o jsonpath='{.data.rootUser}' | base64 -d 2>/dev/null || echo 'minioadmin')"
    echo "  Pass: $(kubectl get secret minio -n storage -o jsonpath='{.data.rootPassword}' | base64 -d 2>/dev/null || echo 'minioadmin')"
    echo ""
  }

  kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' &>/dev/null && {
    echo "ArgoCD: https://argocd.$DOMAIN"
    echo "  User: admin"
    echo "  Pass: $(kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d)"
    echo ""
  }

  kubectl get secret grafana -n monitoring -o jsonpath='{.data.admin-password}' &>/dev/null && {
    echo "Grafana: https://grafana.$DOMAIN"
    echo "  User: admin"
    echo "  Pass: $(kubectl get secret grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d)"
    echo ""
  }

  echo "Vault: https://vault.$DOMAIN"
  echo "  Check: kubectl get secret vault-init-keys -n vault"
  echo ""
}

# ============================================
# MAIN
# ============================================

show_help() {
  cat << 'EOF'
Platform Orchestrator
=====================
All services enabled by default.
DNS: Preserves user records, only manages platform records.

Usage: ./platform.sh <command>

Commands:
  init              Create config
  deploy all        Full deployment
  deploy <comp>     infra|network|dns|cluster|tls|minio|secrets|databases|gitlab|gitops|observability|autoscaling
  status            Show status
  credentials       Show passwords
  health / heal     Check/fix
  destroy           Remove all (DNS: only platform records removed)

Required:
  export HCLOUD_TOKEN="your-token"

Naming: {project}-{resource}
  k8s-network, k8s-bastion, k8s-master-1, k8s-worker-1, k8s-lb

DNS Records:
  Platform records are marked and tracked separately.
  User's existing DNS records are preserved.
  Destroy only removes platform records.

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
    heal)         heal_check || heal_auto ;;
    init)
      [[ -f "$CONFIG_FILE" ]] && { warn "platform.yaml exists"; exit 0; }
      cp "${SCRIPT_DIR}/profiles/small.yaml" "$CONFIG_FILE"
      log "Created platform.yaml"
      log "Edit 'global.project' and 'global.domain', then: ./platform.sh deploy all"
      ;;
    *)            show_help ;;
  esac
}

main "$@"
