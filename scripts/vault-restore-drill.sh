#!/usr/bin/env bash
# Restore a Vault Raft snapshot in an isolated namespace and verify restored data.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
pass() { echo -e "${GREEN}[PASS]${NC} $*"; }
fail() { echo -e "${RED}[FAIL]${NC} $*" >&2; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
info() { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo; echo "── $* ──"; }

SNAPSHOT_BUCKET="${VAULT_SNAPSHOT_BUCKET:-backups/k8s/vault}"
SNAPSHOT_NAME="latest"
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
VAULT_VERSION="${VAULT_VERSION:-2.0.3}"
DRILL_NS="vault-restore-drill"
SOURCE_NS="vault"
TTL_HOURS=24
SKIP_CLEANUP=false
DRY_RUN=false
PASS_COUNT=0
FAIL_COUNT=0

usage() {
  cat <<'EOF'
Usage: vault-restore-drill.sh [options]
  --snapshot-bucket BUCKET  S3 bucket/prefix containing Vault snapshots
  --snapshot-name NAME      Snapshot object name, or latest
  --s3-endpoint URL         S3-compatible endpoint
  --vault-version VERSION   Exact Vault image version
  --namespace NAME          Isolated drill namespace
  --source-namespace NAME   Namespace containing vault-backup-credentials
  --ttl-hours HOURS         Retention label for a preserved drill namespace
  --skip-cleanup            Preserve the drill namespace after execution
  --dry-run                 Print and validate the plan without cluster changes

Actual execution requires VAULT_RESTORE_UNSEAL_KEY, VAULT_RESTORE_TOKEN, and
VAULT_RESTORE_VERIFY_PATH. They must belong to the snapshot being tested.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-bucket) SNAPSHOT_BUCKET="${2:?missing bucket}"; shift 2 ;;
    --snapshot-name) SNAPSHOT_NAME="${2:?missing snapshot name}"; shift 2 ;;
    --s3-endpoint) S3_ENDPOINT="${2:?missing endpoint}"; shift 2 ;;
    --vault-version) VAULT_VERSION="${2:?missing version}"; shift 2 ;;
    --namespace) DRILL_NS="${2:?missing namespace}"; shift 2 ;;
    --source-namespace) SOURCE_NS="${2:?missing source namespace}"; shift 2 ;;
    --ttl-hours) TTL_HOURS="${2:?missing hours}"; shift 2 ;;
    --skip-cleanup) SKIP_CLEANUP=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
done

section "Vault Restore Drill"
info "Namespace: $DRILL_NS"
info "Vault version: $VAULT_VERSION"
info "Snapshot: s3://${SNAPSHOT_BUCKET}/${SNAPSHOT_NAME}"

if [[ "$DRY_RUN" == true ]]; then
  section "DRY-RUN Steps"
  echo "1. [DRY-RUN] Copy scoped S3 credentials from $SOURCE_NS"
  echo "2. [DRY-RUN] Download the selected snapshot to an isolated PVC"
  echo "3. [DRY-RUN] Initialize and unseal a temporary Raft node"
  echo "4. [DRY-RUN] Restore the snapshot with the temporary root token"
  echo "5. [DRY-RUN] Restart and unseal with the snapshot's original key"
  echo "6. [DRY-RUN] Read VAULT_RESTORE_VERIFY_PATH with the snapshot token"
  echo "7. [DRY-RUN] Clean up unless --skip-cleanup is set"
  pass "Plan is non-mutating; supply restore credentials for execution"
  exit 0
fi

section "0. Prerequisites"
for tool in kubectl jq; do
  command -v "$tool" >/dev/null || { fail "$tool is required"; exit 2; }
done
kubectl cluster-info >/dev/null || { fail "Kubernetes cluster is unreachable"; exit 1; }
[[ -n "$S3_ENDPOINT" ]] || { fail "--s3-endpoint or OBJECT_STORAGE_ENDPOINT is required"; exit 2; }
[[ -n "${VAULT_RESTORE_UNSEAL_KEY:-}" ]] || { fail "VAULT_RESTORE_UNSEAL_KEY is required"; exit 2; }
[[ -n "${VAULT_RESTORE_TOKEN:-}" ]] || { fail "VAULT_RESTORE_TOKEN is required"; exit 2; }
[[ -n "${VAULT_RESTORE_VERIFY_PATH:-}" ]] || { fail "VAULT_RESTORE_VERIFY_PATH is required"; exit 2; }
pass "Prerequisites and snapshot restore material are present"

cleanup() {
  if [[ "$SKIP_CLEANUP" == true ]]; then
    warn "Preserving namespace $DRILL_NS; remove it manually after inspection"
  elif kubectl get namespace "$DRILL_NS" >/dev/null 2>&1; then
    kubectl delete namespace "$DRILL_NS" --wait=false
    pass "Cleanup initiated for namespace $DRILL_NS"
  fi
}
trap cleanup EXIT

section "1. Create Isolated Namespace"
if kubectl get namespace "$DRILL_NS" >/dev/null 2>&1; then
  kubectl delete namespace "$DRILL_NS" --wait --timeout=5m
fi
kubectl create namespace "$DRILL_NS"
kubectl label namespace "$DRILL_NS" \
  app.kubernetes.io/part-of=vault-restore-drill \
  backup-restore.io/drill=true \
  backup-restore.io/retention-hours="$TTL_HOURS" --overwrite

kubectl apply -n "$DRILL_NS" -f - <<'EOF'
apiVersion: v1
kind: ResourceQuota
metadata:
  name: vault-restore-drill-quota
spec:
  hard:
    requests.cpu: "2"
    requests.memory: 4Gi
    limits.cpu: "4"
    limits.memory: 8Gi
    pods: "5"
    persistentvolumeclaims: "2"
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vault-restore-data
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: 5Gi
EOF

kubectl get secret vault-backup-credentials -n "$SOURCE_NS" -o json |
  jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields) | .metadata.name="vault-restore-s3"' |
  kubectl apply -n "$DRILL_NS" -f -
kubectl create secret generic vault-restore-credentials -n "$DRILL_NS" \
  --from-literal=restore-unseal-key="$VAULT_RESTORE_UNSEAL_KEY" \
  --from-literal=restore-token="$VAULT_RESTORE_TOKEN"
pass "Isolated namespace, quota, storage, and scoped credentials created"

section "2. Download Snapshot"
if [[ "$SNAPSHOT_NAME" == latest ]]; then
  SNAPSHOT_NAME=$(kubectl run vault-snapshot-list -n "$DRILL_NS" --rm -i --restart=Never \
    --image=amazon/aws-cli:2.34.48 \
    --env="AWS_ENDPOINT_URL=$S3_ENDPOINT" \
    --overrides="$(printf '{\"spec\":{\"containers\":[{\"name\":\"vault-snapshot-list\",\"image\":\"amazon/aws-cli:2.34.48\",\"envFrom\":[{\"secretRef\":{\"name\":\"vault-restore-s3\"}}]}]}}')" \
    --command -- /bin/sh -c \
    "aws --endpoint-url=\"\$AWS_ENDPOINT_URL\" s3 ls s3://${SNAPSHOT_BUCKET}/ | awk '{print \$4}' | sort | tail -1")
  [[ -n "$SNAPSHOT_NAME" ]] || { fail "No snapshot exists in s3://${SNAPSHOT_BUCKET}/"; exit 1; }
  info "Selected latest snapshot: $SNAPSHOT_NAME"
fi

kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: snapshot-downloader
spec:
  backoffLimit: 1
  activeDeadlineSeconds: 600
  template:
    spec:
      restartPolicy: Never
      containers:
        - name: downloader
          image: amazon/aws-cli:2.34.48
          command: ["/bin/sh", "-c"]
          args:
            - >-
              set -euo pipefail;
              aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 cp
              "s3://${SNAPSHOT_BUCKET}/${SNAPSHOT_NAME}" /vault/data/snapshot.snap;
              test -s /vault/data/snapshot.snap
          env:
            - name: AWS_ENDPOINT_URL
              value: "${S3_ENDPOINT}"
          envFrom:
            - secretRef:
                name: vault-restore-s3
          volumeMounts:
            - name: data
              mountPath: /vault/data
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: vault-restore-data
EOF
if ! kubectl wait --for=condition=complete job/snapshot-downloader -n "$DRILL_NS" --timeout=10m; then
  kubectl logs job/snapshot-downloader -n "$DRILL_NS" --all-containers --tail=100
  fail "Snapshot download failed"
  exit 1
fi
pass "Snapshot downloaded into isolated persistent storage"

section "3. Deploy Temporary Vault Raft Node"
kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: vault-drill-config
data:
  vault.hcl: |
    ui = false
    disable_mlock = true
    listener "tcp" {
      tls_disable = 1
      address = "0.0.0.0:8200"
    }
    storage "raft" {
      path = "/vault/data"
      node_id = "vault-drill"
    }
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vault
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: vault
  template:
    metadata:
      labels:
        app.kubernetes.io/name: vault
        app.kubernetes.io/part-of: vault-restore-drill
    spec:
      containers:
        - name: vault
          image: hashicorp/vault:${VAULT_VERSION}
          command: ["/vault/bin/vault"]
          args: ["server", "-config=/vault/config/vault.hcl"]
          env:
            - name: VAULT_ADDR
              value: http://127.0.0.1:8200
            - name: RESTORE_UNSEAL_KEY
              valueFrom:
                secretKeyRef:
                  name: vault-restore-credentials
                  key: restore-unseal-key
            - name: RESTORE_TOKEN
              valueFrom:
                secretKeyRef:
                  name: vault-restore-credentials
                  key: restore-token
          securityContext:
            runAsNonRoot: true
            runAsUser: 100
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: config
              mountPath: /vault/config
            - name: data
              mountPath: /vault/data
      volumes:
        - name: config
          configMap:
            name: vault-drill-config
        - name: data
          persistentVolumeClaim:
            claimName: vault-restore-data
EOF
kubectl rollout status deployment/vault -n "$DRILL_NS" --timeout=3m
pass "Temporary Vault Raft node is running"

section "4. Initialize Temporary Node and Restore Snapshot"
VAULT_INIT=$(kubectl exec -n "$DRILL_NS" deployment/vault -- \
  vault operator init -key-shares=1 -key-threshold=1 -format=json)
TEMP_UNSEAL_KEY=$(jq -er '.unseal_keys_b64[0]' <<<"$VAULT_INIT")
TEMP_ROOT_TOKEN=$(jq -er '.root_token' <<<"$VAULT_INIT")
kubectl exec -n "$DRILL_NS" deployment/vault -- vault operator unseal "$TEMP_UNSEAL_KEY" >/dev/null
kubectl exec -n "$DRILL_NS" deployment/vault -- env VAULT_TOKEN="$TEMP_ROOT_TOKEN" \
  vault operator raft snapshot restore -force /vault/data/snapshot.snap
kubectl rollout restart deployment/vault -n "$DRILL_NS"
kubectl rollout status deployment/vault -n "$DRILL_NS" --timeout=3m
kubectl exec -n "$DRILL_NS" deployment/vault -- \
  vault operator unseal "$RESTORE_UNSEAL_KEY" >/dev/null
SEALED=$(kubectl exec -n "$DRILL_NS" deployment/vault -- vault status -format=json | jq -r .sealed)
[[ "$SEALED" == false ]] || { fail "Restored Vault remains sealed"; exit 1; }
pass "Raft snapshot restored and unsealed with original material"

section "5. Verify Restored Data"
kubectl exec -n "$DRILL_NS" deployment/vault -- env VAULT_TOKEN="$RESTORE_TOKEN" \
  vault kv get "$VAULT_RESTORE_VERIFY_PATH" >/dev/null
TEST_PATH="secret/restore-drill-$(date +%s)"
TEST_VALUE="verified-$(date +%s)"
kubectl exec -n "$DRILL_NS" deployment/vault -- env VAULT_TOKEN="$RESTORE_TOKEN" \
  vault kv put "$TEST_PATH" value="$TEST_VALUE" >/dev/null
RESTORED_TEST_VALUE=$(kubectl exec -n "$DRILL_NS" deployment/vault -- \
  env VAULT_TOKEN="$RESTORE_TOKEN" vault kv get -field=value "$TEST_PATH")
[[ "$RESTORED_TEST_VALUE" == "$TEST_VALUE" ]] || {
  fail "Restored Vault failed the secret round-trip verification"
  exit 1
}
kubectl exec -n "$DRILL_NS" deployment/vault -- env VAULT_TOKEN="$RESTORE_TOKEN" \
  vault kv delete "$TEST_PATH" >/dev/null
pass "Restored path is readable and secret round-trip succeeded"

section "DRILL SUMMARY"
PASS_COUNT=6
echo "Passed: $PASS_COUNT"
echo "Failed: $FAIL_COUNT"
pass "Vault Raft snapshot restore procedure verified"
