#!/usr/bin/env bash
# Offline, backup-gated Vault file-storage to integrated-Raft migration.
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
CONFIG_FILE="$PROJECT_ROOT/platform-orchestrator/platform.yaml"
FORCE=false
DRY_RUN=false

log() { printf '[vault-storage-migrate] %s\n' "$*"; }
fail() { printf '[vault-storage-migrate] ERROR: %s\n' "$*" >&2; exit 1; }
usage() {
  cat <<'EOF'
Usage: vault-storage-migrate.sh [--config FILE] [--force] [--dry-run]

Offline-migrate an existing single-node Vault file backend to integrated Raft.
The caller must complete and record an independent backup gate first. On a
migration failure Vault remains stopped and all source/destination data remains
on the retained PVC for inspected recovery.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config) CONFIG_FILE="${2:?missing config path}"; shift 2 ;;
    --force) FORCE=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "unknown option: $1" ;;
  esac
done

[[ -f "$CONFIG_FILE" ]] || fail "config not found: $CONFIG_FILE"
for tool in kubectl helm jq yq; do command -v "$tool" >/dev/null || fail "$tool is required"; done
project=$(yq -r '.global.project // "k8s"' "$CONFIG_FILE")
marker=vault-storage-migration

if [[ "$DRY_RUN" == true ]]; then
  log "would require a file-backed vault-0 and its retained data PVC"
  log "would stop Vault, migrate file storage offline to /vault/data/raft, and retain evidence"
  log "would uninstall only the Helm release; PVCs and migration evidence remain"
  exit 0
fi

marker_json=$(kubectl get configmap "$marker" -n vault -o json 2>/dev/null || true)
marker_status=$(jq -r '.metadata.annotations["backup.platform.io/status"] // empty' \
  <<<"${marker_json:-{}}" 2>/dev/null || true)
if [[ "$marker_status" == completed ]]; then
  log "migration already completed"
  exit 0
fi

if kubectl get pod vault-0 -n vault >/dev/null 2>&1; then
  status_json=$(kubectl exec -n vault vault-0 -- vault status -format=json 2>/dev/null || true)
  storage_type=$(jq -r '.storage_type // "unknown"' <<<"$status_json" 2>/dev/null || echo unknown)
  if [[ "$storage_type" == raft ]]; then
    log "Vault already uses integrated Raft"
    exit 0
  fi
  [[ "$storage_type" == file ]] || fail "expected Vault file storage, detected: $storage_type"
  pvc=$(kubectl get pod vault-0 -n vault -o json | jq -r '.spec.volumes[] | select(.name=="data") | .persistentVolumeClaim.claimName')
  image=$(kubectl get pod vault-0 -n vault -o jsonpath='{.spec.containers[?(@.name=="vault")].image}')
  replicas=$(kubectl get statefulset vault -n vault -o jsonpath='{.spec.replicas}')
else
  [[ $(kubectl get statefulset vault -n vault -o jsonpath='{.spec.replicas}' 2>/dev/null || echo unknown) == 0 ]] \
    || fail "vault-0 is absent but the Vault StatefulSet is not safely scaled to zero"
  pvc=$(jq -r '.metadata.annotations["backup.platform.io/pvc"] // empty' \
    <<<"${marker_json:-{}}" 2>/dev/null || true)
  image=$(jq -r '.metadata.annotations["backup.platform.io/image"] // empty' \
    <<<"${marker_json:-{}}" 2>/dev/null || true)
  replicas=$(jq -r '.metadata.annotations["backup.platform.io/original-replicas"] // empty' \
    <<<"${marker_json:-{}}" 2>/dev/null || true)
  if [[ -z "$pvc" ]]; then
    claim=$(kubectl get statefulset vault -n vault -o json | jq -r \
      '.spec.volumeClaimTemplates[] | select(.metadata.name == "data") | .metadata.name')
    pvc="${claim:-data}-vault-0"
  fi
  [[ -n "$image" ]] || image=$(kubectl get statefulset vault -n vault \
    -o jsonpath='{.spec.template.spec.containers[?(@.name=="vault")].image}')
  [[ -n "$replicas" ]] || replicas=$(yq -r '.secrets.vault.replicas // 1' "$CONFIG_FILE")
  log "resuming from an already stopped Vault StatefulSet"
fi
[[ -n "$pvc" && "$pvc" != null ]] || fail "Vault data PVC could not be resolved"
[[ -n "$image" ]] || fail "Vault image could not be resolved"
kubectl get pvc "$pvc" -n vault >/dev/null || fail "Vault data PVC is missing: $pvc"

if [[ "$FORCE" != true ]]; then
  printf 'Offline-migrate %s using PVC %s? Type MIGRATE-VAULT: ' "$project" "$pvc"
  read -r confirmation
  [[ "$confirmation" == MIGRATE-VAULT ]] || fail "confirmation did not match MIGRATE-VAULT"
fi

kubectl apply -f - <<EOF >/dev/null
apiVersion: v1
kind: ConfigMap
metadata:
  name: ${marker}
  namespace: vault
  labels:
    app.kubernetes.io/part-of: vault
    app.kubernetes.io/component: storage-migration
  annotations:
    backup.platform.io/status: running
    backup.platform.io/original-replicas: "${replicas}"
    backup.platform.io/pvc: "${pvc}"
    backup.platform.io/image: "${image}"
data:
  migrate.hcl: |-
    storage_source "file" {
      path = "/vault/data/file"
    }
    storage_destination "raft" {
      path = "/vault/data/raft"
      node_id = "vault-0"
    }
    cluster_addr = "https://vault-0.vault-internal:8201"
EOF

log "stopping Vault for an offline storage migration"
kubectl scale statefulset vault -n vault --replicas=0 >/dev/null
# The official Vault chart uses an OnDelete StatefulSet, for which
# `kubectl rollout status` is unsupported. The storage migration only needs a
# hard guarantee that vault-0 has terminated and released the RWO data PVC.
kubectl wait --for=delete pod/vault-0 -n vault --timeout=600s >/dev/null
[[ $(kubectl get statefulset vault -n vault -o jsonpath='{.spec.replicas}') == 0 ]] \
  || fail "Vault StatefulSet did not remain scaled to zero"

kubectl delete job "$marker" -n vault --ignore-not-found --wait=true >/dev/null
kubectl apply -f - <<EOF >/dev/null
apiVersion: batch/v1
kind: Job
metadata:
  name: ${marker}
  namespace: vault
  labels:
    app.kubernetes.io/part-of: vault
    app.kubernetes.io/component: storage-migration
spec:
  backoffLimit: 0
  activeDeadlineSeconds: 1800
  template:
    metadata:
      labels:
        app.kubernetes.io/part-of: vault
        app.kubernetes.io/component: storage-migration
    spec:
      restartPolicy: Never
      automountServiceAccountToken: false
      securityContext:
        runAsNonRoot: true
        runAsUser: 100
        runAsGroup: 1000
        fsGroup: 1000
        seccompProfile:
          type: RuntimeDefault
      containers:
      - name: migrate
        image: ${image}
        securityContext:
          allowPrivilegeEscalation: false
          capabilities:
            drop: ["ALL"]
        command: ["/bin/sh", "-ec"]
        args:
        - |
          mkdir -p /vault/data/file /vault/data/raft
          if [ ! -f /vault/data/.file-layout-moved ]; then
            find /vault/data -mindepth 1 -maxdepth 1 \
              ! -name file ! -name raft ! -name lost+found \
              ! -name .file-layout-moved -exec mv {} /vault/data/file/ \;
            touch /vault/data/.file-layout-moved
          fi
          vault operator migrate -config=/vault/migration/migrate.hcl -max-parallel=2
          test -s /vault/data/raft/vault.db
        volumeMounts:
        - name: data
          mountPath: /vault/data
        - name: migration
          mountPath: /vault/migration
          readOnly: true
        resources:
          requests:
            cpu: 25m
            memory: 64Mi
          limits:
            cpu: 500m
            memory: 512Mi
      volumes:
      - name: data
        persistentVolumeClaim:
          claimName: ${pvc}
      - name: migration
        configMap:
          name: ${marker}
EOF

if ! kubectl wait --for=condition=complete "job/${marker}" -n vault --timeout=1800s; then
  kubectl logs "job/${marker}" -n vault --all-containers --tail=300 >&2 || true
  kubectl annotate configmap "$marker" -n vault backup.platform.io/status=failed --overwrite >/dev/null
  fail "offline Vault migration failed; Vault remains stopped and PVC ${pvc} was retained"
fi
kubectl logs "job/${marker}" -n vault --all-containers --tail=50
kubectl annotate configmap "$marker" -n vault backup.platform.io/status=completed --overwrite >/dev/null

log "removing the old file-backed Helm release while retaining PVCs"
helm uninstall vault -n vault --wait --timeout 10m >/dev/null
kubectl get pvc "$pvc" -n vault >/dev/null || fail "Vault data PVC was not retained"
log "offline migration complete; reconcile the target profile to start integrated Raft"
