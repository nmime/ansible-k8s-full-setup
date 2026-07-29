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

# Prefer the repository-managed runtime so every documented command works from a
# fresh shell without requiring callers to remember to activate the virtualenv.
if [[ -x "${ANSIBLE_DIR}/.venv/bin/ansible-playbook" ]]; then
  PATH="${ANSIBLE_DIR}/.venv/bin:${PATH}"
  export PATH
fi

ENV_LOADER="${ANSIBLE_DIR}/scripts/load-project-env.sh"
if [[ -f "$ENV_LOADER" ]]; then
  # shellcheck source=scripts/load-project-env.sh
  source "$ENV_LOADER"
elif [[ -e "${ANSIBLE_DIR}/.env" ]]; then
  printf 'ERROR: cannot load %s without %s\n' "${ANSIBLE_DIR}/.env" "$ENV_LOADER" >&2
  exit 1
fi

DEFAULT_REGION="hel1"

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
  PROFILE=$(yq '.platform_profile // .tier // "custom"' "$CONFIG_FILE")
  TIER=$(yq '.tier // "small"' "$CONFIG_FILE")
  RESOURCE_TIER=$(yq '.resource_tier // .tier // "small"' "$CONFIG_FILE")
  DOMAIN=$(yq '.global.domain' "$CONFIG_FILE")
  EMAIL=$(yq '.global.email' "$CONFIG_FILE")
  K8S_API_LOCAL_PORT=$(yq '.k8s_api_local_port // 16443' "$CONFIG_FILE")
  [[ -z "$DOMAIN" || "$DOMAIN" == "null" ]] && { error "global.domain is required in $CONFIG_FILE"; exit 1; }
  [[ -z "$EMAIL" || "$EMAIL" == "null" ]] && { error "global.email is required in $CONFIG_FILE"; exit 1; }
  if [[ ! "$K8S_API_LOCAL_PORT" =~ ^[0-9]+$ ]] \
    || (( K8S_API_LOCAL_PORT < 1024 || K8S_API_LOCAL_PORT > 65535 )); then
    error "k8s_api_local_port must be an integer between 1024 and 65535"
    exit 1
  fi
  [[ "$TIER" =~ ^(minimal|small|medium|production)$ ]] || {
    error "Invalid capability tier: $TIER"
    exit 1
  }
  [[ "$PROFILE" =~ ^(minimal|small|medium|medium-optimized|production|custom)$ ]] || {
    error "Invalid platform profile: $PROFILE"
    exit 1
  }
  [[ "$RESOURCE_TIER" =~ ^(minimal|small|medium|production)$ ]] || {
    error "Invalid resource tier: $RESOURCE_TIER"
    exit 1
  }
  case "$PROFILE" in
    minimal|small|medium)
      if [[ "$TIER" != "$PROFILE" || "$RESOURCE_TIER" != "$PROFILE" ]]; then
        error "$PROFILE requires tier=$PROFILE and resource_tier=$PROFILE"
        exit 1
      fi
      ;;
    medium-optimized)
      if [[ "$TIER" != "medium" || "$RESOURCE_TIER" != "small" ]]; then
        error "medium-optimized requires tier=medium and resource_tier=small"
        exit 1
      fi
      ;;
    production)
      if [[ "$TIER" != "production" || "$RESOURCE_TIER" != "small" ]]; then
        error "production requires tier=production and resource_tier=small"
        exit 1
      fi
      ;;
    custom) ;;
  esac
  REGION=$(yq ".infrastructure.region // \"${DEFAULT_REGION}\"" "$CONFIG_FILE")
  log "Config: project=$PROJECT, profile=$PROFILE, tier=$TIER, resource_tier=$RESOURCE_TIER, domain=$DOMAIN, region=$REGION"
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

require_config() {
  [[ -f "$CONFIG_FILE" ]] || { error "Run: ./platform.sh init <profile>"; exit 1; }
  command -v yq >/dev/null 2>&1 || { error "yq v4 is required"; exit 1; }
}

component_path() {
  case "$1" in
    object-storage) echo '.storage.enabled' ;;
    secrets) echo '.secrets.enabled' ;;
    eso) echo '.secrets.eso.enabled' ;;
    databases) echo '.databases.enabled' ;;
    postgresql) echo '.databases.postgresql.enabled' ;;
    mongodb) echo '.databases.mongodb.enabled' ;;
    elasticsearch) echo '.elasticsearch.enabled' ;;
    gitlab) echo '.gitlab.enabled' ;;
    gitlab-runner) echo '.gitlab.runner.enabled' ;;
    gitops) echo '.gitops.enabled' ;;
    observability) echo '.observability.enabled' ;;
    pmm) echo '.observability.pmm.enabled' ;;
    coroot) echo '.coroot.enabled' ;;
    tracing) echo '.tracing.enabled' ;;
    tempo) echo '.tracing.tempo.enabled' ;;
    autoscaling) echo '.autoscaling.enabled' ;;
    dragonfly) echo '.dragonfly.enabled' ;;
    temporal) echo '.temporal.enabled' ;;
    postal) echo '.postal.enabled' ;;
    backup) echo '.backup.enabled' ;;
    disaster-recovery) echo '.backup.disaster_recovery.enabled' ;;
    glitchtip) echo '.glitchtip.enabled' ;;
    apm) echo '.apm.enabled' ;;
    blackbox) echo '.blackbox.enabled' ;;
    daytona) echo '.applications.daytona.enabled' ;;
    hipaa) echo '.compliance.hipaa.enabled' ;;
    *) return 1 ;;
  esac
}

component_selected() {
  local component="$1" path
  if [[ "$component" == tempo ]]; then
    [[ $(yq -r '
      .tracing.tempo.enabled //
      (((.tracing.enabled // false) == true) and
       ((.tracing.backend // "tempo") == "tempo"))
    ' "$CONFIG_FILE") == true ]]
    return
  fi
  path=$(component_path "$component") || return 1
  is_enabled "$path"
}

enable_paths() {
  case "$1" in
    object-storage) echo '.storage.enabled' ;;
    secrets) echo '.secrets.enabled' ;;
    eso) echo '.secrets.enabled .secrets.eso.enabled' ;;
    databases) echo '.databases.enabled' ;;
    postgresql) echo '.databases.enabled .databases.postgresql.enabled' ;;
    mongodb) echo '.databases.enabled .databases.mongodb.enabled' ;;
    elasticsearch) echo '.elasticsearch.enabled' ;;
    gitlab) echo '.storage.enabled .databases.enabled .databases.postgresql.enabled .dragonfly.enabled .gitlab.enabled' ;;
    gitlab-runner) echo '.storage.enabled .databases.enabled .databases.postgresql.enabled .dragonfly.enabled .gitlab.enabled .gitlab.runner.enabled' ;;
    gitops) echo '.gitops.enabled' ;;
    observability) echo '.observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled' ;;
    pmm) echo '.observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .observability.pmm.enabled' ;;
    coroot) echo '.observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .coroot.enabled' ;;
    tracing) echo '.storage.enabled .observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .tracing.enabled' ;;
    tempo) echo '.storage.enabled .observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .tracing.enabled .tracing.tempo.enabled' ;;
    autoscaling) echo '.autoscaling.enabled' ;;
    dragonfly) echo '.dragonfly.enabled' ;;
    temporal) echo '.databases.enabled .databases.postgresql.enabled .temporal.enabled' ;;
    postal) echo '.dragonfly.enabled .postal.enabled' ;;
    backup) echo '.storage.enabled .backup.enabled' ;;
    disaster-recovery) echo '.storage.enabled .backup.enabled .backup.disaster_recovery.enabled' ;;
    glitchtip) echo '.databases.enabled .databases.postgresql.enabled .dragonfly.enabled .glitchtip.enabled' ;;
    apm) echo '.elasticsearch.enabled .apm.enabled' ;;
    blackbox) echo '.observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .blackbox.enabled' ;;
    daytona) echo '.applications.daytona.enabled' ;;
    hipaa) echo '.secrets.enabled .observability.enabled .observability.metrics.enabled .observability.logging.enabled .observability.grafana.enabled .compliance.hipaa.enabled' ;;
    *) return 1 ;;
  esac
}

validate_config() {
  require_config
  ansible-playbook "${ANSIBLE_DIR}/playbooks/validate_profile.yml" -e "@${CONFIG_FILE}"
}

require_component_enabled() {
  local component="$1" path
  path=$(component_path "$component") || { error "Unknown component: $component"; exit 1; }
  component_selected "$component" || {
    error "$component is disabled at $path in platform.yaml"
    echo "Enable it and its dependencies with: ./platform.sh enable $component"
    exit 1
  }
  if [[ "$component" == "databases" ]] && ! is_enabled '.databases.postgresql.enabled' && ! is_enabled '.databases.mongodb.enabled'; then
    error "databases is enabled, but both PostgreSQL and MongoDB are disabled"
    exit 1
  fi
}

show_components() {
  require_config
  printf '%-18s %s\n' COMPONENT ENABLED
  printf '%-18s %s\n' '------------------' '-------'
  local component path value
  for component in object-storage secrets eso databases postgresql mongodb elasticsearch dragonfly gitlab gitlab-runner gitops observability pmm coroot tracing tempo autoscaling temporal postal backup disaster-recovery glitchtip apm blackbox daytona hipaa; do
    path=$(component_path "$component")
    if component_selected "$component"; then value=true; else value=false; fi
    printf '%-18s %s\n' "$component" "$value"
  done
}

enable_component() {
  local component="$1" paths path backup_file
  require_config
  paths=$(enable_paths "$component") || { error "Unknown component: $component"; exit 1; }
  backup_file=$(mktemp "${STATE_DIR}/platform-enable.XXXXXX")
  cp "$CONFIG_FILE" "$backup_file"
  for path in $paths; do
    yq -i "${path} = true" "$CONFIG_FILE"
  done
  if [[ "$component" == tempo ]]; then
    yq -i '.tracing.backend = "tempo"' "$CONFIG_FILE"
  fi
  if ! validate_config; then
    cp "$backup_file" "$CONFIG_FILE"
    rm -f "$backup_file"
    error "Selection was invalid; platform.yaml was restored"
    exit 1
  fi
  rm -f "$backup_file"
  log "Enabled $component and all required dependencies"
  echo "Apply it with: ./platform.sh deploy $component"
}

enabled_blockers() {
  local component="$1" blockers='' path label
  case "$component" in
    object-storage) blockers='.gitlab.enabled:gitlab .backup.enabled:backup' ;;
    databases|postgresql) blockers='.gitlab.enabled:gitlab .temporal.enabled:temporal .glitchtip.enabled:glitchtip' ;;
    elasticsearch) blockers='.apm.enabled:apm' ;;
    dragonfly) blockers='.gitlab.enabled:gitlab .postal.enabled:postal .glitchtip.enabled:glitchtip' ;;
    gitlab) blockers='.gitlab.runner.enabled:gitlab-runner' ;;
    observability) blockers='.observability.pmm.enabled:pmm .coroot.enabled:coroot .tracing.enabled:tracing .blackbox.enabled:blackbox .compliance.hipaa.enabled:hipaa' ;;
    tracing) ;;
    secrets) blockers='.secrets.eso.enabled:eso .compliance.hipaa.enabled:hipaa' ;;
    backup) blockers='.backup.disaster_recovery.enabled:disaster-recovery' ;;
    eso|mongodb|gitlab-runner|gitops|pmm|coroot|tempo|autoscaling|temporal|postal|disaster-recovery|glitchtip|apm|blackbox|daytona|hipaa) ;;
    *) return 1 ;;
  esac
  for label in $blockers; do
    path=${label%%:*}
    if is_enabled "$path"; then
      printf '%s ' "${label#*:}"
    fi
  done
  if [[ "$component" == object-storage ]] && component_selected tempo; then
    printf 'tempo '
  fi
  if [[ "$component" == tracing ]] && component_selected tempo; then
    printf 'tempo '
  fi
}

disable_component() {
  local component="$1" path blockers backup_file
  require_config
  path=$(component_path "$component") || { error "Unknown component: $component"; exit 1; }
  blockers=$(enabled_blockers "$component") || { error "Unknown component: $component"; exit 1; }
  if [[ "$component" == coroot ]] \
    && is_enabled '.tracing.enabled' \
    && [[ $(yq -r '.tracing.backend // "tempo"' "$CONFIG_FILE") == coroot ]]; then
    blockers="${blockers}tracing "
  fi
  if [[ -n "$blockers" ]]; then
    error "Cannot disable $component while these dependants are enabled: $blockers"
    echo "Disable the dependants first. No configuration was changed."
    exit 1
  fi
  backup_file=$(mktemp "${STATE_DIR}/platform-disable.XXXXXX")
  cp "$CONFIG_FILE" "$backup_file"
  yq -i "${path} = false" "$CONFIG_FILE"
  case "$component" in
    secrets) yq -i '.secrets.eso.enabled = false' "$CONFIG_FILE" ;;
    databases) yq -i '.databases.postgresql.enabled = false | .databases.mongodb.enabled = false' "$CONFIG_FILE" ;;
    gitlab) yq -i '.gitlab.runner.enabled = false' "$CONFIG_FILE" ;;
    observability) yq -i '.observability.metrics.enabled = false | .observability.logging.enabled = false | .observability.grafana.enabled = false | .observability.pmm.enabled = false' "$CONFIG_FILE" ;;
    tempo)
      if is_enabled '.coroot.enabled'; then
        yq -i '.tracing.backend = "coroot"' "$CONFIG_FILE"
      else
        yq -i '.tracing.enabled = false' "$CONFIG_FILE"
      fi
      ;;
  esac
  if ! validate_config; then
    cp "$backup_file" "$CONFIG_FILE"
    rm -f "$backup_file"
    error "Selection was invalid; platform.yaml was restored"
    exit 1
  fi
  rm -f "$backup_file"
  log "Disabled $component in platform.yaml"
  warn "Existing Kubernetes resources are unchanged. Use './platform.sh remove $component --confirm $component' only when removal is intended."
}

run_playbook() {
  check_env
  ansible-playbook "${ANSIBLE_DIR}/playbooks/deploy_platform.yml" \
    -e "@${CONFIG_FILE}" \
    -e "platform_profile=${PROFILE}" \
    -e "tier=${TIER}" \
    -e "resource_tier=${RESOURCE_TIER}" \
    -e "project_name=${PROJECT}" \
    -e "domain=${DOMAIN}" \
    -e "email=${EMAIL}" \
    -e "platform_secrets_file=${PLATFORM_SECRETS_FILE:-${ANSIBLE_DIR}/playbooks/.platform-secrets.yml}" \
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
  local require_argocd require_postgresql require_mongodb expected_nodes
  require_argocd=$(flag_from_config '.gitops.enabled' true)
  require_postgresql=$(flag_from_config '.databases.postgresql.enabled' true)
  require_mongodb=$(flag_from_config '.databases.mongodb.enabled' false)
  expected_nodes=$(yq -r '(.infrastructure.control_plane.count // 1) + (.infrastructure.workers.count // 1)' "$CONFIG_FILE")
  HEALTH_REQUIRE_ARGOCD="$require_argocd" \
    HEALTH_REQUIRE_POSTGRESQL="$require_postgresql" \
    HEALTH_REQUIRE_MONGODB="$require_mongodb" \
    HEALTH_EXPECTED_NODES="$expected_nodes" \
    "${ANSIBLE_DIR}/scripts/health-gates.sh"
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
    tls)           run_playbook --tags cluster 2>&1 | tee -a "${LOG_DIR}/tls.log" ;;
    # Storage credential rotation is incomplete until every selected backup
    # consumer has reconciled the same scoped identity.  Include the database
    # tag so pgBackRest/PBM Secrets and their operator CRs cannot retain a
    # credential that SeaweedFS has already replaced.
    object-storage) require_component_enabled "$component"; run_playbook --tags storage,object-storage,seaweedfs,databases 2>&1 | tee -a "${LOG_DIR}/object-storage.log" ;;
    secrets)       require_component_enabled "$component"; run_playbook --tags secrets 2>&1 | tee -a "${LOG_DIR}/secrets.log" ;;
    eso)           require_component_enabled "$component"; run_playbook --tags secrets 2>&1 | tee -a "${LOG_DIR}/eso.log" ;;
    databases)     require_component_enabled "$component"; run_playbook --tags databases 2>&1 | tee -a "${LOG_DIR}/databases.log" ;;
    postgresql)    require_component_enabled "$component"; run_playbook --tags postgresql 2>&1 | tee -a "${LOG_DIR}/postgresql.log" ;;
    mongodb)       require_component_enabled "$component"; run_playbook --tags mongodb 2>&1 | tee -a "${LOG_DIR}/mongodb.log" ;;
    elasticsearch) require_component_enabled "$component"; run_playbook --tags elasticsearch 2>&1 | tee -a "${LOG_DIR}/elasticsearch.log" ;;
    dragonfly)     require_component_enabled "$component"; run_playbook --tags dragonfly 2>&1 | tee -a "${LOG_DIR}/dragonfly.log" ;;
    gitlab)        require_component_enabled "$component"; run_playbook --tags gitlab 2>&1 | tee -a "${LOG_DIR}/gitlab.log" ;;
    gitlab-runner) require_component_enabled "$component"; run_playbook --tags gitlab 2>&1 | tee -a "${LOG_DIR}/gitlab-runner.log" ;;
    gitops)        require_component_enabled "$component"; run_playbook --tags gitops 2>&1 | tee -a "${LOG_DIR}/gitops.log" ;;
    observability) require_component_enabled "$component"; run_playbook --tags monitoring 2>&1 | tee -a "${LOG_DIR}/observability.log" ;;
    pmm)           require_component_enabled "$component"; run_playbook --tags monitoring 2>&1 | tee -a "${LOG_DIR}/pmm.log" ;;
    coroot)        require_component_enabled "$component"; run_playbook --tags coroot 2>&1 | tee -a "${LOG_DIR}/coroot.log" ;;
    tracing)       require_component_enabled "$component"; run_playbook --tags monitoring 2>&1 | tee -a "${LOG_DIR}/tracing.log" ;;
    tempo)         require_component_enabled "$component"; run_playbook --tags monitoring 2>&1 | tee -a "${LOG_DIR}/tempo.log" ;;
    autoscaling)   require_component_enabled "$component"; run_playbook --tags autoscaling 2>&1 | tee -a "${LOG_DIR}/autoscaling.log" ;;
    temporal)      require_component_enabled "$component"; run_playbook --tags temporal 2>&1 | tee -a "${LOG_DIR}/temporal.log" ;;
    postal)        require_component_enabled "$component"; run_playbook --tags postal 2>&1 | tee -a "${LOG_DIR}/postal.log" ;;
    backup)        require_component_enabled "$component"; run_playbook --tags databases,gitlab,backup 2>&1 | tee -a "${LOG_DIR}/backup.log" ;;
    disaster-recovery) require_component_enabled "$component"; run_playbook --tags databases,gitlab,backup 2>&1 | tee -a "${LOG_DIR}/disaster-recovery.log" ;;
    glitchtip)     require_component_enabled "$component"; run_playbook --tags glitchtip 2>&1 | tee -a "${LOG_DIR}/glitchtip.log" ;;
    apm)           require_component_enabled "$component"; run_playbook --tags apm 2>&1 | tee -a "${LOG_DIR}/apm.log" ;;
    blackbox)      require_component_enabled "$component"; run_playbook --tags blackbox 2>&1 | tee -a "${LOG_DIR}/blackbox.log" ;;
    daytona)       require_component_enabled "$component"; deploy_daytona ;;
    hipaa)         require_component_enabled "$component"; run_playbook --tags network,monitoring,hipaa 2>&1 | tee -a "${LOG_DIR}/hipaa.log" ;;
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
  local base_domain
  base_domain=$(yq -r '.applications.daytona.base_domain // ""' "$CONFIG_FILE")
  [[ -n "$base_domain" ]] || base_domain="daytona.${DOMAIN}"
  run_playbook \
    -e "deploy_daytona=true" \
    -e "daytona_base_domain=${base_domain}" \
    --tags daytona 2>&1 | tee -a "${LOG_DIR}/daytona.log"
}

remove_component() {
  local component="${1:-}" confirmation='' delete_data=false path
  shift || true
  [[ -n "$component" ]] || { error "Component name is required"; exit 1; }
  path=$(component_path "$component") || { error "Unknown component: $component"; exit 1; }
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --confirm)
        [[ $# -ge 2 ]] || { error "--confirm requires the component name"; exit 1; }
        confirmation="${2:-}"
        shift 2
        ;;
      --delete-data)
        delete_data=true
        shift
        ;;
      *)
        error "Unknown remove option: $1"
        exit 1
        ;;
    esac
  done
  component_selected "$component" && {
    error "$component is still enabled at $path. Run './platform.sh disable $component' first."
    exit 1
  }
  [[ "$confirmation" == "$component" ]] || {
    error "Removal requires exact confirmation: --confirm $component"
    exit 1
  }
  ansible-playbook "${ANSIBLE_DIR}/playbooks/remove_component.yml" \
    -e "@${CONFIG_FILE}" \
    -e "project_name=${PROJECT}" \
    -e "target_component=${component}" \
    -e "confirm_component_removal=${confirmation}" \
    -e "delete_component_data=${delete_data}" \
    2>&1 | tee -a "${LOG_DIR}/remove-${component}.log"
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
  is_enabled '.coroot.enabled' && records+=$'\n'"coroot IN 3600 A ${ip}"
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
  check_env
  "${ANSIBLE_DIR}/teardown.sh" "$PROJECT" --confirm "$PROJECT" \
    --api-port "$K8S_API_LOCAL_PORT" 2>&1 | tee -a "${LOG_DIR}/destroy.log"
  rm -rf "${STATE_DIR:?}"/*
  log "Destroy verified. DNS zone/records and your global kubeconfig were preserved."
}

show_status() {
  echo "Platform: ${PROJECT:-k8s} / ${DOMAIN:-unknown} [${REGION:-unknown}]"
  echo "Profile: ${PROFILE:-unknown}; capability tier: ${TIER:-unknown}; resource tier: ${RESOURCE_TIER:-unknown}"
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
    local daytona_host
    daytona_host=$(yq -r '.applications.daytona.base_domain // ""' "$CONFIG_FILE")
    [[ -n "$daytona_host" ]] || daytona_host="daytona.${DOMAIN}"
    echo "Daytona: https://${daytona_host}"
  fi
  if is_enabled '.coroot.enabled'; then
    echo "Coroot (VPN/admin gateway): https://coroot.${DOMAIN}"
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
  components        Show every selectable technology and its current state
  enable <comp>     Enable a technology and its required dependencies in YAML
  disable <comp>    Disable a technology; refuses while dependants are enabled
  validate          Validate the selection and dependency contract offline
  deploy all        Full deployment
  deploy <comp>     infra|network|dns|cluster|tls or any component shown above
  remove <comp>     Remove disabled resources; exact --confirm is mandatory
                    Add --delete-data only to permit deletion of PVC-backed data
  status            Show status
  credentials       Show passwords
  health / heal     Check/fix
  backup-cluster    Create encrypted etcd/config/PVC disaster-recovery backup
  restore-cluster   Verify or restore an encrypted full-cluster backup
  migrate           All-to-all named profile migration; plan/execute require --target
  destroy           Remove project-prefixed Hetzner resources; preserve DNS and kubeconfig

Required:
  export HCLOUD_TOKEN="your-token"

Profiles:
  minimal | small | medium | medium-optimized | production
EOF
}

main() {
  local cmd="${1:-help}"
  shift || true
  case "$cmd" in
    deploy)       load_config; deploy_component "${1:-all}" ;;
    components)   show_components ;;
    enable)       enable_component "${1:-}" ;;
    disable)      disable_component "${1:-}" ;;
    validate)     validate_config ;;
    remove)       load_config; remove_component "$@" ;;
    destroy)      load_config; destroy_all ;;
    status)       load_config 2>/dev/null || true; show_status ;;
    credentials)  load_config; show_credentials ;;
    health)       heal_check ;;
    heal)         heal_check; heal_auto ;;
    backup-cluster)
      require_config
      exec "${ANSIBLE_DIR}/scripts/cluster-backup.sh" --config "$CONFIG_FILE" "$@"
      ;;
    restore-cluster)
      exec "${ANSIBLE_DIR}/scripts/cluster-restore.sh" "$@"
      ;;
    migrate)
      require_config
      exec "${ANSIBLE_DIR}/scripts/migrate-profile.sh" --config "$CONFIG_FILE" "$@"
      ;;
    init)         init_config "${1:-example}" ;;
    *)            show_help ;;
  esac
}

main "$@"
