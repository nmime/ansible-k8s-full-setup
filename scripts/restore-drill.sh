#!/usr/bin/env bash
# Dispatches component restore drills without pretending unsupported restores work.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"
COMPONENT=""
BACKUP_REF=""
DRY_RUN=false
FORCE=false
RESTORE_NS=""
CLEANUP_HOURS=""

usage() {
  cat <<'EOF'
Usage: restore-drill.sh --component <mongodb|vault|gitlab> --backup <ref> [--dry-run|--force]

Supported automated drills:
  mongodb Dispatches to mongodb-restore-drill.sh with a backup CR name or S3 URI.
  vault   Dispatches to vault-restore-drill.sh with the snapshot name.
  gitlab  Dispatches to gitlab-restore-test.sh with the Toolbox backup ID.

SeaweedFS restore automation is provided only by the replacement-cluster
Velero workflow. This component dispatcher fails closed for SeaweedFS.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --component) COMPONENT="${2:?missing component}"; shift 2 ;;
    --backup) BACKUP_REF="${2:?missing backup reference}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --force) FORCE=true; shift ;;
    --namespace) RESTORE_NS="${2:?missing namespace}"; shift 2 ;;
    --cleanup-hours) CLEANUP_HOURS="${2:?missing cleanup hours}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "[ERROR] Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$COMPONENT" ]] || { echo "[ERROR] Missing --component" >&2; exit 2; }
[[ -n "$BACKUP_REF" ]] || { echo "[ERROR] Missing --backup" >&2; exit 2; }
if [[ "$DRY_RUN" != true && "$FORCE" != true ]]; then
  echo "[ERROR] Use --dry-run to review or --force to execute a restore drill" >&2
  exit 2
fi

common_args=()
[[ -n "$RESTORE_NS" ]] && common_args+=(--namespace "$RESTORE_NS")
[[ -n "$CLEANUP_HOURS" ]] && common_args+=(--ttl-hours "$CLEANUP_HOURS")
[[ "$DRY_RUN" == true ]] && common_args+=(--dry-run)

echo "RESTORE DRILL SUMMARY"
echo "Component: $COMPONENT"
echo "Backup: $BACKUP_REF"

case "$COMPONENT" in
  mongodb)
    exec "${SCRIPT_DIR}/mongodb-restore-drill.sh" \
      --backup "$BACKUP_REF" "${common_args[@]}"
    ;;
  vault)
    exec "${SCRIPT_DIR}/vault-restore-drill.sh" \
      --snapshot-name "$BACKUP_REF" "${common_args[@]}"
    ;;
  gitlab)
    exec "${SCRIPT_DIR}/gitlab-restore-test.sh" \
      --restore --backup "$BACKUP_REF" "${common_args[@]}"
    ;;
  seaweedfs)
    echo "[ERROR] No verified isolated $COMPONENT restore implementation exists" >&2
    exit 2
    ;;
  *)
    echo "[ERROR] Invalid component: $COMPONENT" >&2
    exit 2
    ;;
esac
