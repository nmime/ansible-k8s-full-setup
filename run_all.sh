#!/usr/bin/env bash

# Prepare five isolated controllers, then deploy every named profile in parallel.
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROFILES="minimal small medium medium-optimized production"

usage() {
  cat <<'EOF'
Usage: ./run_all.sh [options]

Options:
  --campaign-id ID       Unique ID (default: UTC timestamp)
  --campaign-root DIR    Worktrees, homes, configs, logs, and results
  --source-ref REF       Git commit used by every controller (default: HEAD)
  --project-prefix NAME  Prefix for all five Hetzner projects
  --base-domain DOMAIN   Parent test domain (default: n0xeid.xyz)
  --email EMAIL          ACME/operator email
  --api-port-base PORT   First of five unique API tunnel ports (default: 16443)
  --dr-endpoint URL      External S3-compatible DR endpoint
  --dr-bucket NAME       External DR bucket; prefixes remain per-project
  --certificate-issuer   ClusterIssuer (default: letsencrypt-staging)
  --manage-dns           Permit each profile deployment to manage DNS
  --minimum-storage      Use 10Gi profile-controlled PVC requests
  --dry-run              Generate five runtime configs, but do not use Git/cloud
  -h, --help             Show this help

Successful deployments are intentionally retained for load and recovery tests.
The runner never tears down cloud resources or deletes evidence automatically.
EOF
}

die() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }
require_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "$1 requires a value"; }

CAMPAIGN_ID="${CAMPAIGN_ID:-$(date -u +%Y%m%dT%H%M%SZ)}"
CAMPAIGN_ROOT="${CAMPAIGN_ROOT:-}"
SOURCE_REF="${SOURCE_REF:-HEAD}"
PROJECT_PREFIX="${PROJECT_PREFIX:-}"
BASE_DOMAIN="${BASE_DOMAIN:-n0xeid.xyz}"
EMAIL="${EMAIL:-admin@n0xeid.xyz}"
API_PORT_BASE="${API_PORT_BASE:-16443}"
DR_ENDPOINT="${BACKUP_DR_ENDPOINT:-}"
DR_BUCKET="${BACKUP_DR_BUCKET:-}"
MANAGE_DNS=false
MINIMUM_STORAGE=false
CERTIFICATE_ISSUER="${CERT_MANAGER_CLUSTER_ISSUER:-letsencrypt-staging}"
DRY_RUN=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --campaign-id) require_value "$1" "${2:-}"; CAMPAIGN_ID="$2"; shift 2 ;;
    --campaign-root) require_value "$1" "${2:-}"; CAMPAIGN_ROOT="$2"; shift 2 ;;
    --source-ref) require_value "$1" "${2:-}"; SOURCE_REF="$2"; shift 2 ;;
    --project-prefix) require_value "$1" "${2:-}"; PROJECT_PREFIX="$2"; shift 2 ;;
    --base-domain) require_value "$1" "${2:-}"; BASE_DOMAIN="$2"; shift 2 ;;
    --email) require_value "$1" "${2:-}"; EMAIL="$2"; shift 2 ;;
    --api-port-base) require_value "$1" "${2:-}"; API_PORT_BASE="$2"; shift 2 ;;
    --dr-endpoint) require_value "$1" "${2:-}"; DR_ENDPOINT="$2"; shift 2 ;;
    --dr-bucket) require_value "$1" "${2:-}"; DR_BUCKET="$2"; shift 2 ;;
    --certificate-issuer) require_value "$1" "${2:-}"; CERTIFICATE_ISSUER="$2"; shift 2 ;;
    --manage-dns) MANAGE_DNS=true; shift ;;
    --minimum-storage) MINIMUM_STORAGE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option '$1'" ;;
  esac
done

[[ "$CAMPAIGN_ID" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || die "invalid campaign id '$CAMPAIGN_ID'"
[[ "$BASE_DOMAIN" =~ ^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$ && "$BASE_DOMAIN" == *.* ]] \
  || die "invalid base domain '$BASE_DOMAIN'"
[[ "$EMAIL" == *@*.* ]] || die "invalid email '$EMAIL'"
[[ "$CERTIFICATE_ISSUER" =~ ^[a-z0-9][a-z0-9-]{0,62}$ ]] || die "invalid certificate issuer"
if [[ ! "$API_PORT_BASE" =~ ^[0-9]+$ ]] || ((API_PORT_BASE < 1024 || API_PORT_BASE > 65531)); then
  die "invalid API port base '$API_PORT_BASE'"
fi

CAMPAIGN_SHORT="$(printf '%s' "$CAMPAIGN_ID" | tr '[:upper:]_.' '[:lower:]--' | tr -cd 'a-z0-9-' | cut -c1-14)"
[[ -n "$CAMPAIGN_SHORT" ]] || die "campaign id does not produce a safe project fragment"
PROJECT_PREFIX="${PROJECT_PREFIX:-t5-${CAMPAIGN_SHORT}}"
[[ "$PROJECT_PREFIX" =~ ^[a-z0-9][a-z0-9-]{1,27}$ ]] \
  || die "project prefix must be 2-28 lowercase letters, numbers, or hyphens"
CAMPAIGN_ROOT="${CAMPAIGN_ROOT:-/private/tmp/ansible-k8s-five-profile-${CAMPAIGN_ID}}"

# shellcheck source=scripts/load-project-env.sh
# shellcheck disable=SC1091
source "${SCRIPT_DIR}/scripts/load-project-env.sh"
DR_ENDPOINT="${DR_ENDPOINT:-${BACKUP_DR_ENDPOINT:-}}"
DR_BUCKET="${DR_BUCKET:-${BACKUP_DR_BUCKET:-}}"

command -v git >/dev/null 2>&1 || die "git is required"
command -v yq >/dev/null 2>&1 || die "yq v4 is required"
command -v jq >/dev/null 2>&1 || die "jq is required"
[[ ! -e "$CAMPAIGN_ROOT" ]] || die "campaign root already exists: $CAMPAIGN_ROOT"
mkdir -p "$CAMPAIGN_ROOT/controllers" "$CAMPAIGN_ROOT/results"
CAMPAIGN_ROOT="$(cd "$CAMPAIGN_ROOT" && pwd)"
chmod 700 "$CAMPAIGN_ROOT" "$CAMPAIGN_ROOT/controllers" "$CAMPAIGN_ROOT/results"

SOURCE_SHA="$(git -C "$SCRIPT_DIR" rev-parse --verify "${SOURCE_REF}^{commit}")" \
  || die "source ref does not resolve: $SOURCE_REF"
PROJECT_ENV_FILE="${PROJECT_ENV_LOADED:-${SCRIPT_DIR}/.env}"
export PROJECT_ENV_FILE
if [[ -x "${SCRIPT_DIR}/.venv/bin/ansible-playbook" ]]; then
  PATH="${SCRIPT_DIR}/.venv/bin:${PATH}"
  export PATH
fi

MANIFEST="$CAMPAIGN_ROOT/manifest.tsv"
SUMMARY="$CAMPAIGN_ROOT/summary.tsv"
printf 'profile\tproject\tdomain\tapi_port\tworktree\trun_root\n' >"$MANIFEST"
printf 'profile\tstatus\texit_code\tlog\n' >"$SUMMARY"

PIDS=()
NAMES=()
LOGS=()
WORKTREES=()
RUN_ROOTS=()
RUNNERS=()
PROJECTS=()
DOMAINS=()
API_PORTS=()
HOMES=()
CONFIGS=()
DEPLOY_LOGS=()

# shellcheck disable=SC2329 # Invoked by the INT/TERM trap.
terminate_children() {
  local pid
  printf '\nInterrupt received; stopping campaign controller processes only. Cloud resources are retained.\n' >&2
  for pid in "${PIDS[@]}"; do
    kill "$pid" 2>/dev/null || true
  done
  for pid in "${PIDS[@]}"; do
    wait "$pid" 2>/dev/null || true
  done
  exit 130
}
trap terminate_children INT TERM

index=0
for profile in $PROFILES; do
  project="${PROJECT_PREFIX}-${profile}"
  domain="${project}.${BASE_DOMAIN}"
  api_port=$((API_PORT_BASE + index))
  controller="$CAMPAIGN_ROOT/controllers/$profile"
  worktree="$controller/worktree"
  run_root="$controller/state"
  home="$controller/home"
  config="$run_root/platform.yaml"
  log="$run_root/logs/deploy.log"
  console_log="$CAMPAIGN_ROOT/results/${profile}.console.log"
  mkdir -p "$controller" "$run_root" "$home" "$(dirname "$log")"
  chmod 700 "$controller" "$run_root" "$home"

  if $DRY_RUN; then
    worktree="$SCRIPT_DIR"
    runner="$SCRIPT_DIR/run_tier.sh"
  else
    git -C "$SCRIPT_DIR" worktree add --detach "$worktree" "$SOURCE_SHA" >/dev/null
    runner="$worktree/run_tier.sh"
    [[ -x "$runner" ]] || die "source commit does not contain an executable run_tier.sh: $SOURCE_SHA"
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$profile" "$project" "$domain" "$api_port" "$worktree" "$run_root" >>"$MANIFEST"

  NAMES+=("$profile")
  WORKTREES+=("$worktree")
  RUN_ROOTS+=("$run_root")
  RUNNERS+=("$runner")
  PROJECTS+=("$project")
  DOMAINS+=("$domain")
  API_PORTS+=("$api_port")
  HOMES+=("$home")
  CONFIGS+=("$config")
  DEPLOY_LOGS+=("$log")
  LOGS+=("$console_log")
  index=$((index + 1))
done

# Nothing is launched until all five isolated controllers have been prepared.
index=0
for profile in $PROFILES; do
  project="${PROJECTS[$index]}"
  domain="${DOMAINS[$index]}"
  api_port="${API_PORTS[$index]}"
  run_root="${RUN_ROOTS[$index]}"
  home="${HOMES[$index]}"
  config="${CONFIGS[$index]}"
  log="${DEPLOY_LOGS[$index]}"
  console_log="${LOGS[$index]}"
  runner="${RUNNERS[$index]}"
  args=(
    "$profile"
    --campaign-id "$CAMPAIGN_ID"
    --project "$project"
    --domain "$domain"
    --email "$EMAIL"
    --home "$home"
    --run-root "$run_root"
    --config "$config"
    --log-file "$log"
    --api-port "$api_port"
    --dr-prefix "${project}/velero"
    --dns-zone "$BASE_DOMAIN"
    --certificate-issuer "$CERTIFICATE_ISSUER"
  )
  [[ -z "$DR_ENDPOINT" ]] || args+=(--dr-endpoint "$DR_ENDPOINT")
  [[ -z "$DR_BUCKET" ]] || args+=(--dr-bucket "$DR_BUCKET")
  $MANAGE_DNS && args+=(--manage-dns)
  $MINIMUM_STORAGE && args+=(--minimum-storage)
  $DRY_RUN && args+=(--dry-run)

  (
    export PROJECT_ENV_FILE
    "$runner" "${args[@]}"
  ) >"$console_log" 2>&1 &
  PIDS+=("$!")
  index=$((index + 1))
done

printf 'Campaign %s launched %s profiles concurrently from %s\n' "$CAMPAIGN_ID" "$index" "$SOURCE_SHA"
printf 'Manifest: %s\n' "$MANIFEST"

overall_rc=0
index=0
for pid in "${PIDS[@]}"; do
  profile="${NAMES[$index]}"
  console_log="${LOGS[$index]}"
  if wait "$pid"; then
    rc=0
    status="PASS"
  else
    rc=$?
    status="FAIL"
    overall_rc=1
  fi
  printf '%s\t%s\t%s\t%s\n' "$profile" "$status" "$rc" "$console_log" >>"$SUMMARY"
  printf '%-18s %-4s rc=%s log=%s\n' "$profile" "$status" "$rc" "$console_log"
  index=$((index + 1))
done
trap - INT TERM

printf '\nSummary: %s\n' "$SUMMARY"
if [[ "$overall_rc" -eq 0 ]]; then
  if $DRY_RUN; then
    printf 'All five isolated runtime plans passed. No cloud resources were changed.\n'
  else
    printf 'All five deployments passed. Keep the controllers for load, backup, and restore evidence.\n'
  fi
else
  printf 'One or more profiles failed. No automatic teardown was attempted; inspect logs before cleanup.\n' >&2
fi

if ! $DRY_RUN; then
  printf '\nCleanup guidance (run only after evidence capture):\n'
  index=0
  for project in "${PROJECTS[@]}"; do
    printf '  HOME=%q KUBECONFIG=%q %q %q --confirm %q\n' \
      "${HOMES[$index]}" "${HOMES[$index]}/.kube/config" \
      "${WORKTREES[$index]}/teardown.sh" "$project" "$project"
    index=$((index + 1))
  done
  printf 'Then remove retained controller worktrees and campaign state:\n'
  for worktree in "${WORKTREES[@]}"; do
    printf '  git -C %q worktree remove %q\n' "$SCRIPT_DIR" "$worktree"
  done
  printf '  rm -rf %q\n' "$CAMPAIGN_ROOT"
  printf 'DNS records/zones are preserved by teardown and must be reviewed separately.\n'
fi
exit "$overall_rc"
