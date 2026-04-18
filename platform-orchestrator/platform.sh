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
  EMAIL=$(yq '.global.email' "$CONFIG_FILE")

  [[ -z "$DOMAIN" || "$DOMAIN" == "null" ]] && { error "domain is required in config. Set global.domain in $CONFIG_FILE"; exit 1; }
  [[ -z "$EMAIL" || "$EMAIL" == "null" ]] && { error "email is required in config. Set global.email in $CONFIG_FILE"; exit 1; }
  REGION=$(yq ".infrastructure.region // \"${DEFAULT_REGION}\"" "$CONFIG_FILE")

  NETWORK_NAME="${PROJECT}-network"
  BASTION_NAME="${PROJECT}-bastion"
  LB_NAME="${PROJECT}-lb"

  log "Config: project=$PROJECT, tier=$TIER, domain=$DOMAIN, region=$REGION"
}

is_enabled() {
  local path="$1"
  local value
  value=$(yq "${path} // false" "$CONFIG_FILE" 2>/dev/null) || return 1
  [[ "$value" == "true" ]]
}

# ============================================
# HEALTH
# ============================================

heal_check() {
  log "Health check..."
  local issues=0
  local unhealthy
  unhealthy=$(kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' || true)
  if [[ -n "$unhealthy" ]]; then
    warn "Unhealthy pods:"
    echo "$unhealthy"
    ((issues++)) || true
  fi
  [[ $issues -eq 0 ]] && log "All healthy!" || warn "$issues issue(s) found"
  return 0
}

heal_auto() {
  warn "Auto-healing: will delete unhealthy pods"
  local unhealthy
  unhealthy=$(kubectl get pods -A --no-headers 2>/dev/null | grep -vE 'Running|Completed' || true)
  [[ -z "$unhealthy" ]] && { log "No unhealthy pods"; return 0; }
  echo "$unhealthy" | while read -r ns name _; do
    warn "Deleting: $ns/$name"
    kubectl delete pod "$name" -n "$ns" --grace-period=30 2>/dev/null || true
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
    "opwerf")       deploy_opwerf ;;
    "e2b")          deploy_e2b ;;
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
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
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

  is_enabled '.e2b.enabled' && records+="
e2b-api IN 3600 A ${ip}
sandbox IN 3600 A ${ip}"

  echo "$records"
}

deploy_network() {
  log "Deploying network security (VPN + bastion hardening)..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags network \
    2>&1 | tee -a "${LOG_DIR}/network.log"
}

deploy_cluster() {
  log "Deploying K8s cluster..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags cluster \
    2>&1 | tee -a "${LOG_DIR}/cluster.log"
}

deploy_tls() {
  log "Setting up TLS..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags tls \
    2>&1 | tee -a "${LOG_DIR}/tls.log"
}

deploy_minio() {
  is_enabled '.storage.enabled' || { log "MinIO: disabled"; return 0; }
  log "Installing MinIO..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags storage \
    2>&1 | tee -a "${LOG_DIR}/minio.log"
}

deploy_secrets() {
  is_enabled '.secrets.enabled' || { log "Vault: disabled"; return 0; }
  log "Installing Vault..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags secrets \
    2>&1 | tee -a "${LOG_DIR}/secrets.log"
}

deploy_databases() {
  is_enabled '.databases.postgresql.enabled' || { log "PostgreSQL: disabled"; return 0; }
  log "Installing PostgreSQL..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags databases \
    2>&1 | tee -a "${LOG_DIR}/databases.log"
}

deploy_gitlab() {
  is_enabled '.gitlab.enabled' || { log "GitLab: disabled"; return 0; }
  log "Installing GitLab..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags gitlab \
    2>&1 | tee -a "${LOG_DIR}/gitlab.log"
}

deploy_gitops() {
  is_enabled '.gitops.enabled' || { log "ArgoCD: disabled"; return 0; }
  log "Installing ArgoCD..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags gitops \
    2>&1 | tee -a "${LOG_DIR}/gitops.log"
}

deploy_observability() {
  is_enabled '.observability.metrics.enabled' || { log "Observability: disabled"; return 0; }
  log "Installing monitoring..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags observability \
    2>&1 | tee -a "${LOG_DIR}/observability.log"
}

deploy_autoscaling() {
  is_enabled '.autoscaling.enabled' || { log "KEDA: disabled"; return 0; }
  log "Installing KEDA..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    --tags autoscaling \
    2>&1 | tee -a "${LOG_DIR}/autoscaling.log"
}

deploy_opwerf() {
  log "Installing OpenWerf (st/pp/prod)..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    -e "deploy_opwerf=true" \
    --tags opwerf \
    2>&1 | tee -a "${LOG_DIR}/opwerf.log"
}

deploy_e2b() {
  is_enabled '.e2b.enabled' || { log "E2B: disabled"; return 0; }
  log "Deploying E2B Sandbox Infrastructure..."
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    -e "deploy_e2b=true" \
    --tags e2b \
    2>&1 | tee -a "${LOG_DIR}/e2b.log"
}

# ============================================
# DESTROY
# ============================================

destroy_all() {
  warn "DESTROY project '$PROJECT'?"
  warn "DNS: Only platform records removed, your records preserved"
  read -rp "Type 'DESTROY': " confirm
  [[ "$confirm" != "DESTROY" ]] && exit 0

  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "tier=${TIER}" -e "project_name=${PROJECT}" -e "domain=${DOMAIN}" -e "email=${EMAIL}" \
    -e "state=absent" \
    2>&1 | tee -a "${LOG_DIR}/destroy.log"

  rm -f ~/.kube/config
  rm -rf "${STATE_DIR:?}"/*
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
  kubectl get secret pmm-secret -n monitoring -o jsonpath='{.data.PMM_ADMIN_PASSWORD}' &>/dev/null && {
    echo "PMM: https://pmm.$DOMAIN"
    echo "  User: admin"
    echo "  Pass: $(kubectl get secret pmm-secret -n monitoring -o jsonpath='{.data.PMM_ADMIN_PASSWORD}' | base64 -d)"
    echo ""
  }

  kubectl get secret elasticsearch-es-elastic-user -n logging -o jsonpath='{.data.elastic}' &>/dev/null && {
    echo "Elasticsearch/Kibana: https://kibana.$DOMAIN"
    echo "  User: elastic"
    echo "  Pass: $(kubectl get secret elasticsearch-es-elastic-user -n logging -o jsonpath='{.data.elastic}' | base64 -d)"
    echo ""
  }

  echo "PostgreSQL:"
  echo "  Host: ${PROJECT_NAME:-k8s}-pg-pgbouncer.databases.svc.cluster.local:5432"
  echo "  Creds: kubectl get secret ${PROJECT_NAME:-k8s}-pg-pguser-app -n databases -o jsonpath='{.data.password}' | base64 -d"
  echo ""

  echo "MongoDB:"
  echo "  Host: ${PROJECT_NAME:-k8s}-mongo-rs0.databases.svc.cluster.local:27017"
  echo "  Creds: kubectl get secret internal-${PROJECT_NAME:-k8s}-mongo-users -n databases -o jsonpath='{.data.APP_USER_PASSWORD}' | base64 -d"
  echo ""

  echo "Dragonfly (Redis):"
  echo "  Host: dragonfly.dragonfly.svc.cluster.local:6379"
  echo "  Creds: kubectl get secret dragonfly-auth -n dragonfly -o jsonpath='{.data.password}' | base64 -d"
  echo ""

  echo "Temporal: https://temporal.$DOMAIN"
  echo "  gRPC: temporal-frontend.temporal.svc.cluster.local:7233"
  echo ""

  echo "OpenWerf (multi-environment):"
  echo "  Production: https://app.$DOMAIN + https://api.$DOMAIN"
  echo "  Pre-prod:   https://pp-app.$DOMAIN + https://pp-api.$DOMAIN"
  echo "  Staging:    https://st-app.$DOMAIN + https://st-api.$DOMAIN"
  echo "  Namespace:  opwerf / opwerf-pp / opwerf-st"
  echo "  ArgoCD:     opwerf-production / opwerf-pp / opwerf-st"
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
    heal)         heal_check; heal_auto ;;
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
