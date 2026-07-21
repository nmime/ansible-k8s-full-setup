#!/usr/bin/env bash
# Capture a fail-closed configuration baseline for upgrade rollback.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
PLATFORM_CONFIG_FILE="${PLATFORM_CONFIG_FILE:-${PROJECT_ROOT}/platform-orchestrator/platform.yaml}"
SNAPSHOT_DIR="${SNAPSHOT_DIR:-${PROJECT_ROOT}/snapshot}"
SNAPSHOT_DRY_RUN="${SNAPSHOT_DRY_RUN:-false}"

log() { printf '[%s] %s\n' "$(date +'%Y-%m-%d %H:%M:%S')" "$*"; }
dry() { printf '[DRY-RUN] %s\n' "$*"; }

parse_snapshot_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --dry-run) SNAPSHOT_DRY_RUN=true ;;
      --config)
        [[ $# -ge 2 && -n "$2" ]] || { printf '%s\n' '--config requires a file' >&2; return 2; }
        PLATFORM_CONFIG_FILE="$2"
        shift
        ;;
      --snapshot-dir)
        [[ $# -ge 2 && -n "$2" ]] || { printf '%s\n' '--snapshot-dir requires a directory' >&2; return 2; }
        SNAPSHOT_DIR="$2"
        shift
        ;;
      *) printf 'Unknown option: %s\n' "$1" >&2; return 2 ;;
    esac
    shift
  done
}

capture_snapshot() (
  local timestamp snap_dir releases_json namespace release revision config_file snapshot_root config_source_json
  umask 077
  mkdir -p "$SNAPSHOT_DIR"
  snapshot_root="$(cd "$SNAPSHOT_DIR" && pwd)"
  timestamp=$(date +'%Y%m%d-%H%M%S')
  snap_dir="${snapshot_root}/upgrade-${timestamp}"

  if [[ "$SNAPSHOT_DRY_RUN" == "true" ]]; then
    log "Capturing rollback baseline to $snap_dir"
    dry "Would capture platform config, exact Helm revisions/values/manifests, and cluster objects"
    printf '%s\n' "$snap_dir"
    return 0
  fi

  [[ -f "$PLATFORM_CONFIG_FILE" ]] || {
    printf 'platform config is required for a rollback baseline: %s\n' "$PLATFORM_CONFIG_FILE" >&2
    return 1
  }
  config_file="$(cd "$(dirname "$PLATFORM_CONFIG_FILE")" && pwd)/$(basename "$PLATFORM_CONFIG_FILE")"
  command -v helm >/dev/null
  command -v kubectl >/dev/null
  kubectl cluster-info >/dev/null
  snap_dir="$(mktemp -d "${snapshot_root}/upgrade-${timestamp}-XXXXXX")"
  log "Capturing rollback baseline to $snap_dir"
  mkdir -p "$snap_dir"/{helm-values,helm-manifests,rbac}
  cp "$config_file" "$snap_dir/platform.yaml"
  config_source_json=$(jq -Rn --arg path "$config_file" '$path')

  releases_json=$(helm list --all-namespaces --output json)
  printf '%s\n' "$releases_json" > "$snap_dir/helm-releases.json"
  while IFS=$'\t' read -r namespace release revision; do
    [[ -n "$release" ]] || continue
    helm get values "$release" --namespace "$namespace" --all > "$snap_dir/helm-values/${namespace}-${release}.yaml"
    helm get manifest "$release" --namespace "$namespace" > "$snap_dir/helm-manifests/${namespace}-${release}.yaml"
    printf '%s\t%s\t%s\n' "$namespace" "$release" "$revision" >> "$snap_dir/helm-revisions.tsv"
  done < <(printf '%s' "$releases_json" | jq -r '.[] | [.namespace,.name,(.revision|tostring)] | @tsv')

  kubectl get crds -o yaml > "$snap_dir/crds.yaml"
  kubectl get namespaces -o yaml > "$snap_dir/namespaces.yaml"
  kubectl get clusterroles -o yaml > "$snap_dir/rbac/clusterroles.yaml"
  kubectl get clusterrolebindings -o yaml > "$snap_dir/rbac/clusterrolebindings.yaml"
  kubectl get pv -o yaml > "$snap_dir/pvs.yaml"
  kubectl get pvc --all-namespaces -o yaml > "$snap_dir/pvcs.yaml"
  kubectl get nodes -o yaml > "$snap_dir/nodes.yaml"
  kubectl version -o yaml > "$snap_dir/version.yaml"

  cat > "$snap_dir/MANIFEST.yaml" <<MANIFEST
snapshot_time: "$timestamp"
snapshot_kind: configuration-baseline
platform_config: platform.yaml
platform_config_source: $config_source_json
helm_revisions: helm-revisions.tsv
data_backup_required: true
MANIFEST

  ln -sfn "$snap_dir" "${snapshot_root}/latest"
  log "Snapshot saved to $snap_dir"
  printf '%s\n' "$snap_dir"
)

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  parse_snapshot_args "$@"
  mkdir -p "$SNAPSHOT_DIR"
  capture_snapshot
fi
