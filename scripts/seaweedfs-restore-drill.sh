#!/usr/bin/env bash
# Restore one SeaweedFS data volume from a Velero/Kopia backup into an
# isolated, dynamically provisioned PVC and verify it without starting a
# replacement SeaweedFS cluster.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"

BACKUP_NAME=""
RESTORE_NS="seaweedfs-restore-drill"
SOURCE_NS="storage"
SOURCE_POD="seaweedfs-master-0"
SOURCE_VOLUME="data-storage"
STORAGE_SIZE="4Gi"
TTL_HOURS=24
DRY_RUN=false
SKIP_CLEANUP=false
RESTORE_NAME=""

usage() {
  cat <<'EOF'
Usage: seaweedfs-restore-drill.sh --backup BACKUP [options]

Options:
  --namespace NAME       Isolated target namespace (default: seaweedfs-restore-drill)
  --source-namespace NS  Source namespace recorded in the backup (default: storage)
  --source-pod NAME      Exact backed-up SeaweedFS pod (default: seaweedfs-master-0)
  --source-volume NAME   Exact backed-up pod volume (default: data-storage)
  --storage-size SIZE    Replacement PVC request (default: 4Gi)
  --ttl-hours HOURS      Cleanup-tracking label (default: 24)
  --dry-run              Validate and print the plan without mutating the cluster
  --skip-cleanup         Retain disposable restore resources for investigation
  --help                 Show this help

The script restores one completed Kopia PodVolumeBackup. It never starts a
second SeaweedFS quorum: the restored application pod is used only until
Velero's restore-wait init container finishes, then a network-isolated,
read-only verifier checks the replacement PVC.
EOF
}

info() { printf '[INFO] %s\n' "$*"; }
pass() { printf '[PASS] %s\n' "$*"; }
fail() { printf '[FAIL] %s\n' "$*" >&2; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP_NAME="${2:?missing backup}"; shift 2 ;;
    --namespace) RESTORE_NS="${2:?missing namespace}"; shift 2 ;;
    --source-namespace) SOURCE_NS="${2:?missing source namespace}"; shift 2 ;;
    --source-pod) SOURCE_POD="${2:?missing source pod}"; shift 2 ;;
    --source-volume) SOURCE_VOLUME="${2:?missing source volume}"; shift 2 ;;
    --storage-size) STORAGE_SIZE="${2:?missing storage size}"; shift 2 ;;
    --ttl-hours) TTL_HOURS="${2:?missing ttl hours}"; shift 2 ;;
    --dry-run) DRY_RUN=true; shift ;;
    --skip-cleanup) SKIP_CLEANUP=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
done

[[ -n "$BACKUP_NAME" ]] || { fail "--backup is required"; exit 2; }
for value in "$RESTORE_NS" "$SOURCE_NS" "$SOURCE_POD" "$SOURCE_VOLUME"; do
  [[ "$value" =~ ^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$ ]] || {
    fail "Kubernetes names may contain only lowercase letters, digits, dots, and dashes: $value"
    exit 2
  }
done
[[ "$BACKUP_NAME" =~ ^[A-Za-z0-9._-]+$ ]] || { fail "Invalid backup name"; exit 2; }
[[ "$STORAGE_SIZE" =~ ^[1-9][0-9]*(Mi|Gi|Ti)$ ]] || {
  fail "--storage-size must be a positive Kubernetes binary quantity such as 4Gi"
  exit 2
}
[[ "$TTL_HOURS" =~ ^[1-9][0-9]*$ ]] || { fail "--ttl-hours must be a positive integer"; exit 2; }
for tool in kubectl jq; do
  command -v "$tool" >/dev/null || { fail "$tool is required"; exit 2; }
done

RESTORE_NAME="seaweedfs-drill-$(date -u +%Y%m%dt%H%M%Sz | tr '[:upper:]' '[:lower:]')"
RESTORE_CREATED=false
NAMESPACE_CREATED=false

cleanup() {
  local rc=$?
  if [[ "$DRY_RUN" == false && "$SKIP_CLEANUP" == false ]]; then
    if [[ "$RESTORE_CREATED" == true ]]; then
      kubectl delete restore -n velero "$RESTORE_NAME" --wait=false >/dev/null 2>&1 || true
    fi
    if [[ "$NAMESPACE_CREATED" == true ]]; then
      kubectl delete namespace "$RESTORE_NS" --wait --timeout=300s >/dev/null 2>&1 || true
    fi
  elif [[ "$SKIP_CLEANUP" == true ]]; then
    info "Retained namespace $RESTORE_NS and restore $RESTORE_NAME"
  fi
  exit "$rc"
}
trap cleanup EXIT

backup_phase=$(kubectl get backup -n velero "$BACKUP_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || true)
[[ "$backup_phase" == "Completed" ]] || {
  fail "Velero backup $BACKUP_NAME is not Completed (phase: ${backup_phase:-missing})"
  exit 1
}

pvb_json=$(kubectl get podvolumebackups -n velero \
  -l "velero.io/backup-name=${BACKUP_NAME}" -o json)
matching_pvbs=$(jq --arg ns "$SOURCE_NS" --arg pod "$SOURCE_POD" --arg volume "$SOURCE_VOLUME" \
  '[.items[] | select(.spec.pod.namespace == $ns and .spec.pod.name == $pod and .spec.volume == $volume and .status.phase == "Completed")]' \
  <<<"$pvb_json")
pvb_count=$(jq 'length' <<<"$matching_pvbs")
[[ "$pvb_count" -eq 1 ]] || {
  fail "Expected exactly one completed PodVolumeBackup for ${SOURCE_NS}/${SOURCE_POD}:${SOURCE_VOLUME}; found $pvb_count"
  exit 1
}
snapshot_id=$(jq -r '.[0].status.snapshotID' <<<"$matching_pvbs")
backup_bytes=$(jq -r '.[0].status.progress.totalBytes' <<<"$matching_pvbs")
source_pvc=$(jq -r '.[0].metadata.annotations["velero.io/pvc-name"]' <<<"$matching_pvbs")
[[ -n "$snapshot_id" && "$snapshot_id" != null && "$backup_bytes" =~ ^[1-9][0-9]*$ ]] || {
  fail "The selected PodVolumeBackup has no non-empty Kopia snapshot"
  exit 1
}

source_pod_json=$(kubectl get pod -n "$SOURCE_NS" "$SOURCE_POD" -o json)
claim_from_pod=$(jq -r --arg volume "$SOURCE_VOLUME" \
  '.spec.volumes[] | select(.name == $volume) | .persistentVolumeClaim.claimName // empty' \
  <<<"$source_pod_json")
[[ "$claim_from_pod" == "$source_pvc" ]] || {
  fail "Backup PVC $source_pvc does not match source pod volume claim ${claim_from_pod:-missing}"
  exit 1
}
source_pvc_json=$(kubectl get pvc -n "$SOURCE_NS" "$source_pvc" -o json)
storage_class=$(jq -r '.spec.storageClassName // empty' <<<"$source_pvc_json")
access_mode=$(jq -r '.spec.accessModes[0] // empty' <<<"$source_pvc_json")
service_account=$(jq -r '.spec.serviceAccountName // "default"' <<<"$source_pod_json")
source_image=$(jq -r --arg volume "$SOURCE_VOLUME" \
  '[.spec.containers[] | select(any(.volumeMounts[]?; .name == $volume))][0].image // empty' \
  <<<"$source_pod_json")
mount_path=$(jq -r --arg volume "$SOURCE_VOLUME" \
  '[.spec.containers[].volumeMounts[]? | select(.name == $volume)][0].mountPath // empty' \
  <<<"$source_pod_json")
[[ -n "$storage_class" && -n "$access_mode" && -n "$source_image" && -n "$mount_path" ]] || {
  fail "Could not derive storage class, access mode, image, or mount path from the source workload"
  exit 1
}

info "Backup: $BACKUP_NAME (Completed)"
info "Kopia snapshot: $snapshot_id ($backup_bytes bytes)"
info "Source: ${SOURCE_NS}/${SOURCE_POD}:${SOURCE_VOLUME} -> PVC $source_pvc"
info "Target: ${RESTORE_NS}/${source_pvc} (${STORAGE_SIZE}, ${storage_class})"
if [[ "$DRY_RUN" == true ]]; then
  pass "Dry-run prerequisites and exact backup relationship verified"
  exit 0
fi

if kubectl get namespace "$RESTORE_NS" >/dev/null 2>&1; then
  info "Removing previous disposable namespace $RESTORE_NS"
  kubectl delete namespace "$RESTORE_NS" --wait --timeout=300s >/dev/null
fi
kubectl create namespace "$RESTORE_NS" >/dev/null
NAMESPACE_CREATED=true
kubectl label namespace "$RESTORE_NS" restore-drill/ttl-hours="$TTL_HOURS" --overwrite >/dev/null
kubectl annotate namespace "$RESTORE_NS" restore-drill/created="$(date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite >/dev/null

kubectl apply -n "$RESTORE_NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: seaweedfs-restore-drill
spec:
  hard:
    pods: "3"
    persistentvolumeclaims: "1"
    requests.storage: ${STORAGE_SIZE}
---
apiVersion: v1
kind: ServiceAccount
metadata:
  name: ${service_account}
automountServiceAccountToken: false
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: ${source_pvc}
spec:
  accessModes: ["${access_mode}"]
  storageClassName: ${storage_class}
  resources:
    requests:
      storage: ${STORAGE_SIZE}
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: deny-all
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
EOF

# Copy only ConfigMaps directly mounted by the selected source pod. Secrets are
# deliberately not copied; this drill validates stored bytes without joining a
# replacement quorum or exposing application credentials.
while IFS= read -r config_map; do
  [[ -n "$config_map" ]] || continue
  kubectl get configmap -n "$SOURCE_NS" "$config_map" -o json \
    | jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields)' \
    | kubectl apply -n "$RESTORE_NS" -f - >/dev/null
done < <(jq -r '.spec.volumes[]? | .configMap.name // empty' <<<"$source_pod_json" | sort -u)

kubectl apply -n velero -f - >/dev/null <<EOF
apiVersion: velero.io/v1
kind: Restore
metadata:
  name: ${RESTORE_NAME}
spec:
  backupName: ${BACKUP_NAME}
  includedNamespaces: ["${SOURCE_NS}"]
  namespaceMapping:
    ${SOURCE_NS}: ${RESTORE_NS}
  labelSelector:
    matchLabels:
      statefulset.kubernetes.io/pod-name: ${SOURCE_POD}
  includeClusterResources: false
  restorePVs: false
  existingResourcePolicy: none
  itemOperationTimeout: 30m
EOF
RESTORE_CREATED=true

info "Waiting for Velero restore $RESTORE_NAME"
deadline=$((SECONDS + 2400))
while (( SECONDS < deadline )); do
  phase=$(kubectl get restore -n velero "$RESTORE_NAME" -o jsonpath='{.status.phase}' 2>/dev/null || true)
  case "$phase" in
    Completed) break ;;
    PartiallyFailed|Failed|FailedValidation)
      fail "Velero restore entered terminal phase $phase"
      exit 1
      ;;
  esac
  sleep 5
done
[[ "${phase:-}" == "Completed" ]] || { fail "Timed out waiting for Velero restore"; exit 1; }

restore_json=$(kubectl get restore -n velero "$RESTORE_NAME" -o json)
restore_errors=$(jq -r '.status.errors // 0' <<<"$restore_json")
restore_warnings=$(jq -r '.status.warnings // 0' <<<"$restore_json")
[[ "$restore_errors" -eq 0 ]] || { fail "Velero reported $restore_errors restore errors"; exit 1; }

velero_pod=$(kubectl get pods -n velero -l name=velero -o jsonpath='{.items[0].metadata.name}')
restore_details=$(kubectl exec -n velero "$velero_pod" -- /velero restore describe "$RESTORE_NAME" --details)
allowed_warning_count=$(grep -Ec 'could not restore, PersistentVolumeClaim .* already exists|failed to list stub VolumeGroupSnapshotContents' <<<"$restore_details" || true)
[[ "$allowed_warning_count" -eq "$restore_warnings" ]] || {
  fail "Velero reported a warning outside the two documented replacement-PVC/optional-group-snapshot cases"
  printf '%s\n' "$restore_details" >&2
  exit 1
}

pvr_json=$(kubectl get podvolumerestores -n velero -l "velero.io/restore-name=${RESTORE_NAME}" -o json)
pvr_count=$(jq '.items | length' <<<"$pvr_json")
[[ "$pvr_count" -eq 1 ]] || { fail "Expected exactly one PodVolumeRestore; found $pvr_count"; exit 1; }
pvr_phase=$(jq -r '.items[0].status.phase // empty' <<<"$pvr_json")
pvr_snapshot=$(jq -r '.items[0].spec.snapshotID // empty' <<<"$pvr_json")
pvr_bytes=$(jq -r '.items[0].status.progress.bytesDone // 0' <<<"$pvr_json")
[[ "$pvr_phase" == "Completed" && "$pvr_snapshot" == "$snapshot_id" && "$pvr_bytes" -eq "$backup_bytes" ]] || {
  fail "PodVolumeRestore did not reproduce the selected snapshot exactly"
  exit 1
}

restored_pod_json=$(kubectl get pod -n "$RESTORE_NS" "$SOURCE_POD" -o json)
restore_uid=$(jq -r '.metadata.uid' <<<"$restore_json")
restore_wait_exit=$(jq -r '[.status.initContainerStatuses[]? | select(.name == "restore-wait")][0].state.terminated.exitCode // -1' <<<"$restored_pod_json")
[[ "$restore_wait_exit" -eq 0 ]] || { fail "Velero restore-wait init container did not exit successfully"; exit 1; }

# Stop the restored application container before mounting its PVC in the
# verifier. This avoids forming an accidental second quorum against source DNS.
kubectl delete pod -n "$RESTORE_NS" "$SOURCE_POD" --wait --timeout=180s >/dev/null

kubectl apply -n "$RESTORE_NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: seaweedfs-restore-verifier
spec:
  automountServiceAccountToken: false
  restartPolicy: Never
  activeDeadlineSeconds: 300
  securityContext:
    runAsUser: 0
    runAsGroup: 0
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: verify
      image: ${source_image}
      imagePullPolicy: IfNotPresent
      command: ["/bin/sh", "-ec"]
      args:
        - >-
          test -f '${mount_path}/.velero/${restore_uid}';
          files=\$(find '${mount_path}' -type f ! -path '${mount_path}/.velero/*' | wc -l | tr -d ' ');
          bytes=\$(find '${mount_path}' -type f ! -path '${mount_path}/.velero/*' -exec wc -c {} + | awk 'END {print \$1}');
          test "\$files" -gt 0;
          test "\$bytes" = '${backup_bytes}';
          printf 'verified-files=%s verified-bytes=%s\n' "\$files" "\$bytes"
      securityContext:
        allowPrivilegeEscalation: false
        capabilities:
          drop: ["ALL"]
        readOnlyRootFilesystem: true
      volumeMounts:
        - name: restored-data
          mountPath: ${mount_path}
          readOnly: true
  volumes:
    - name: restored-data
      persistentVolumeClaim:
        claimName: ${source_pvc}
        readOnly: true
EOF

if ! kubectl wait -n "$RESTORE_NS" pod/seaweedfs-restore-verifier \
  --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null; then
  kubectl logs -n "$RESTORE_NS" seaweedfs-restore-verifier --tail=100 >&2 || true
  fail "Restored data verifier failed"
  exit 1
fi
verifier_output=$(kubectl logs -n "$RESTORE_NS" seaweedfs-restore-verifier)
printf '%s\n' "$verifier_output"
pass "Velero/Kopia restored snapshot $snapshot_id byte-for-byte into an isolated replacement PVC"
pass "Restore errors: 0; allow-listed warnings: $restore_warnings; application network access: denied"
