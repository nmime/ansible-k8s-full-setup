#!/usr/bin/env bash
# Restore a Vault Raft snapshot in an isolated namespace and verify restored data.
# Remote command bodies are intentionally single-quoted so the disposable
# Vault pod, not this workstation shell, expands their variables.
# shellcheck disable=SC2016
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

wait_for_ready_vault_pod() {
  local deadline pod
  deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    pod=$(kubectl get pods -n "$DRILL_NS" \
      -l app.kubernetes.io/name=vault -o json \
      | jq -r '[.items[] | select(.metadata.deletionTimestamp == null) | select(any(.status.conditions[]?; .type == "Ready" and .status == "True"))] | sort_by(.metadata.creationTimestamp) | last | .metadata.name // empty')
    if [[ -n "$pod" ]]; then
      printf '%s\n' "$pod"
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_vault_api() {
  local pod="$1" deadline status
  deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    status=$(kubectl exec -n "$DRILL_NS" "pod/$pod" -- \
      vault status -format=json 2>/dev/null) || true
    if printf '%s' "$status" | jq -e \
      'has("initialized") and has("sealed")' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

wait_for_vault_active() {
  local pod="$1" deadline status
  deadline=$((SECONDS + 180))
  while (( SECONDS < deadline )); do
    status=$(kubectl exec -n "$DRILL_NS" "pod/$pod" -- \
      vault status -format=json 2>/dev/null) || true
    if printf '%s' "$status" | jq -e \
      '.is_self == true and (.leader_address // "") != ""' >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

SNAPSHOT_BUCKET="${VAULT_SNAPSHOT_BUCKET:-backups/k8s/vault}"
SNAPSHOT_NAME="latest"
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
VAULT_VERSION="${VAULT_VERSION:-2.0.3}"
DRILL_NS="vault-restore-drill"
SOURCE_NS="vault"
RESTORE_CREDENTIALS_SECRET=""
TTL_HOURS=24
STORAGE_SIZE="${VAULT_RESTORE_STORAGE_SIZE:-10Gi}"
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
  --credentials-secret NAME Copy restore-unseal-keys and restore-token from this source Secret
  --storage-size SIZE       Isolated Raft PVC size (default: 10Gi)
  --ttl-hours HOURS         Retention label for a preserved drill namespace
  --skip-cleanup            Preserve the drill namespace after execution
  --dry-run                 Print and validate the plan without cluster changes

Actual execution requires VAULT_RESTORE_VERIFY_PATH plus either a source
credentials Secret or VAULT_RESTORE_UNSEAL_KEY and VAULT_RESTORE_TOKEN. A
credentials Secret should contain restore-token and newline-delimited
restore-unseal-keys; restore-unseal-key remains supported for one-share Vaults.
The credentials must belong to the snapshot being tested.
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
    --credentials-secret) RESTORE_CREDENTIALS_SECRET="${2:?missing secret name}"; shift 2 ;;
    --storage-size) STORAGE_SIZE="${2:?missing storage size}"; shift 2 ;;
    --ttl-hours) TTL_HOURS="${2:?missing hours}"; shift 2 ;;
    --skip-cleanup) SKIP_CLEANUP=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) fail "Unknown option: $1"; usage >&2; exit 2 ;;
  esac
done

section "Vault Restore Drill"
[[ "$STORAGE_SIZE" =~ ^[1-9][0-9]*(Ei|Pi|Ti|Gi|Mi|Ki|E|P|T|G|M|K)$ ]] \
  || { fail "--storage-size must be a positive Kubernetes storage quantity such as 10Gi"; exit 2; }
info "Namespace: $DRILL_NS"
info "Vault version: $VAULT_VERSION"
info "Storage size: $STORAGE_SIZE"
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
if [[ -n "$RESTORE_CREDENTIALS_SECRET" ]]; then
  kubectl get secret "$RESTORE_CREDENTIALS_SECRET" -n "$SOURCE_NS" >/dev/null \
    || { fail "Restore credentials Secret $SOURCE_NS/$RESTORE_CREDENTIALS_SECRET was not found"; exit 2; }
else
  [[ -n "${VAULT_RESTORE_UNSEAL_KEY:-}" ]] || { fail "VAULT_RESTORE_UNSEAL_KEY is required"; exit 2; }
  [[ -n "${VAULT_RESTORE_TOKEN:-}" ]] || { fail "VAULT_RESTORE_TOKEN is required"; exit 2; }
fi
[[ -n "${VAULT_RESTORE_VERIFY_PATH:-}" ]] || { fail "VAULT_RESTORE_VERIFY_PATH is required"; exit 2; }
pass "Prerequisites and snapshot restore material are present"

cleanup() {
  if [[ "$SKIP_CLEANUP" == true ]]; then
    warn "Preserving namespace $DRILL_NS; remove it manually after inspection"
  elif kubectl get namespace "$DRILL_NS" >/dev/null 2>&1; then
    kubectl delete namespace "$DRILL_NS" --wait=true --timeout=10m
    pass "Cleanup completed for namespace $DRILL_NS"
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

kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: vault-restore-drill-quota
spec:
  hard:
    pods: "5"
    persistentvolumeclaims: "2"
    requests.storage: ${STORAGE_SIZE}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: vault-restore-data
spec:
  accessModes: ["ReadWriteOnce"]
  resources:
    requests:
      storage: ${STORAGE_SIZE}
EOF

kubectl get secret vault-backup-credentials -n "$SOURCE_NS" -o json |
  jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields) | .metadata.name="vault-restore-s3"' |
  kubectl apply -n "$DRILL_NS" -f -
if [[ -n "$RESTORE_CREDENTIALS_SECRET" ]]; then
  kubectl get secret "$RESTORE_CREDENTIALS_SECRET" -n "$SOURCE_NS" -o json \
    | jq 'del(.metadata.namespace,.metadata.resourceVersion,.metadata.uid,.metadata.creationTimestamp,.metadata.ownerReferences,.metadata.managedFields) | .metadata.name="vault-restore-credentials"' \
    | kubectl apply -n "$DRILL_NS" -f -
else
  kubectl create secret generic vault-restore-credentials -n "$DRILL_NS" \
    --from-literal=restore-unseal-key="$VAULT_RESTORE_UNSEAL_KEY" \
    --from-literal=restore-token="$VAULT_RESTORE_TOKEN"
fi
pass "Isolated namespace, quota, storage, and scoped credentials created"

section "2. Download Snapshot"
if [[ "$SNAPSHOT_NAME" == latest ]]; then
  kubectl apply -n "$DRILL_NS" -f - >/dev/null <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: vault-snapshot-list
spec:
  restartPolicy: Never
  containers:
    - name: list
      image: amazon/aws-cli:2.34.48
      command: ["/bin/sh", "-c"]
      args:
        - >-
          aws --endpoint-url="\$AWS_ENDPOINT_URL" s3 ls s3://${SNAPSHOT_BUCKET}/
          | awk '{print \$4}' | sort | tail -1
      env:
        - name: AWS_ENDPOINT_URL
          value: "${S3_ENDPOINT}"
      envFrom:
        - secretRef:
            name: vault-restore-s3
EOF
  if ! kubectl wait -n "$DRILL_NS" pod/vault-snapshot-list \
    --for=jsonpath='{.status.phase}'=Succeeded --timeout=5m >/dev/null; then
    kubectl logs -n "$DRILL_NS" pod/vault-snapshot-list --tail=100 >&2 || true
    fail "Could not list Vault snapshots"
    exit 1
  fi
  SNAPSHOT_NAME=$(kubectl logs -n "$DRILL_NS" pod/vault-snapshot-list | tail -1)
  kubectl delete pod vault-snapshot-list -n "$DRILL_NS" --wait=true >/dev/null
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
kubectl delete job snapshot-downloader -n "$DRILL_NS" --wait=true >/dev/null
kubectl apply -n "$DRILL_NS" -f - <<'EOF'
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: vault-restore-network-isolation
spec:
  podSelector: {}
  policyTypes: [Ingress, Egress]
EOF
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
    api_addr = "http://127.0.0.1:8200"
    cluster_addr = "http://127.0.0.1:8201"
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
  strategy:
    type: Recreate
  selector:
    matchLabels:
      app.kubernetes.io/name: vault
  template:
    metadata:
      labels:
        app.kubernetes.io/name: vault
        app.kubernetes.io/part-of: vault-restore-drill
    spec:
      securityContext:
        runAsNonRoot: true
        runAsUser: 100
        runAsGroup: 1000
        fsGroup: 1000
        fsGroupChangePolicy: OnRootMismatch
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: vault
          image: hashicorp/vault:${VAULT_VERSION}
          command: ["vault"]
          args: ["server", "-config=/vault/config/vault.hcl"]
          env:
            - name: VAULT_ADDR
              value: http://127.0.0.1:8200
          securityContext:
            runAsNonRoot: true
            runAsUser: 100
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            seccompProfile:
              type: RuntimeDefault
            capabilities:
              drop: ["ALL"]
          volumeMounts:
            - name: config
              mountPath: /vault/config
            - name: data
              mountPath: /vault/data
            - name: restore-credentials
              mountPath: /vault/restore-credentials
              readOnly: true
            - name: audit
              mountPath: /vault/audit
      volumes:
        - name: config
          configMap:
            name: vault-drill-config
        - name: data
          persistentVolumeClaim:
            claimName: vault-restore-data
        - name: restore-credentials
          secret:
            secretName: vault-restore-credentials
            defaultMode: 0400
        - name: audit
          emptyDir:
            sizeLimit: 256Mi
EOF
kubectl rollout status deployment/vault -n "$DRILL_NS" --timeout=3m
VAULT_POD=$(wait_for_ready_vault_pod) || {
  fail "Temporary Vault pod did not become ready"
  exit 1
}
wait_for_vault_api "$VAULT_POD" || {
  fail "Temporary Vault API did not become reachable"
  exit 1
}
pass "Temporary Vault Raft node is running"

section "4. Initialize Temporary Node and Restore Snapshot"
# Keep the temporary initialization material entirely inside the disposable
# pod. It never appears in local argv, process environments, or command output.
kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c '
  set -eu
  umask 077
  init=$(vault operator init -key-shares=1 -key-threshold=1)
  temp_unseal=$(printf "%s\n" "$init" | awk "/^Unseal Key 1:/ {print \$NF}")
  temp_token=$(printf "%s\n" "$init" | awk "/^Initial Root Token:/ {print \$NF}")
  test -n "$temp_unseal" && test -n "$temp_token"
  vault operator unseal "$temp_unseal" >/dev/null
  VAULT_TOKEN="$temp_token" vault operator raft snapshot restore -force /vault/data/snapshot.snap
  # A production snapshot retains its original Raft voters. This disposable
  # one-node recovery must explicitly reform quorum before it can elect itself.
  # Vault consumes and removes peers.json on the next process start.
  mkdir -p /vault/data/raft
  printf "%s\n" "[{\"id\":\"vault-drill\",\"address\":\"127.0.0.1:8201\",\"non_voter\":false}]" \
    > /vault/data/raft/peers.json
  chmod 0600 /vault/data/raft/peers.json
  unset init temp_unseal temp_token
'
kubectl rollout restart deployment/vault -n "$DRILL_NS"
kubectl rollout status deployment/vault -n "$DRILL_NS" --timeout=3m
VAULT_POD=$(wait_for_ready_vault_pod) || {
  fail "Restored Vault pod did not become ready"
  exit 1
}
wait_for_vault_api "$VAULT_POD" || {
  fail "Restored Vault API did not become reachable"
  exit 1
}
kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c '
  set -eu
  keys=/vault/restore-credentials/restore-unseal-keys
  single=/vault/restore-credentials/restore-unseal-key
  if [ -s "$keys" ]; then
    while IFS= read -r key || [ -n "$key" ]; do
      [ -n "$key" ] || continue
      status=$(vault operator unseal -format=json "$key")
      if printf "%s" "$status" | grep -q "\"sealed\"[[:space:]]*:[[:space:]]*false"; then
        unset key status
        exit 0
      fi
    done < "$keys"
  elif [ -s "$single" ]; then
    status=$(vault operator unseal -format=json "$(cat "$single")")
    if printf "%s" "$status" | grep -q "\"sealed\"[[:space:]]*:[[:space:]]*false"; then
      unset status
      exit 0
    fi
  fi
  unset key status 2>/dev/null || true
  exit 1
' || {
  fail "Restored Vault rejected its original unseal material"
  exit 1
}
SEALED=$(kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- vault status -format=json | jq -r .sealed)
[[ "$SEALED" == false ]] || { fail "Restored Vault remains sealed"; exit 1; }
wait_for_vault_active "$VAULT_POD" || {
  fail "Restored Vault did not reform single-node quorum and become active"
  exit 1
}
PEERS=$(kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c \
  'VAULT_TOKEN=$(cat /vault/restore-credentials/restore-token); export VAULT_TOKEN; vault operator raft list-peers -format=json')
printf '%s' "$PEERS" | jq -e \
  '[.data.config.servers[] | select(.leader == true and .voter == true)] | length == 1' \
  >/dev/null || {
    fail "Restored Vault peer set does not contain exactly one voting leader"
    exit 1
  }
unset PEERS
pass "Raft snapshot restored and unsealed with original material"

section "5. Verify Restored Data"
kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c \
  'VAULT_TOKEN=$(cat /vault/restore-credentials/restore-token); export VAULT_TOKEN; vault kv get "$1" >/dev/null' -- "$VAULT_RESTORE_VERIFY_PATH"
TEST_PATH="secret/restore-drill-$(date +%s)"
TEST_VALUE="verified-$(date +%s)"
# The command is evaluated inside the restored Vault pod, not by this shell.
# shellcheck disable=SC2016
kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c \
  'VAULT_TOKEN=$(cat /vault/restore-credentials/restore-token); export VAULT_TOKEN; vault kv put "$1" value="$2" >/dev/null' -- "$TEST_PATH" "$TEST_VALUE"
# shellcheck disable=SC2016
RESTORED_TEST_VALUE=$(kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- \
  sh -c 'VAULT_TOKEN=$(cat /vault/restore-credentials/restore-token); export VAULT_TOKEN; vault kv get -field=value "$1"' -- "$TEST_PATH")
[[ "$RESTORED_TEST_VALUE" == "$TEST_VALUE" ]] || {
  fail "Restored Vault failed the secret round-trip verification"
  exit 1
}
# shellcheck disable=SC2016
kubectl exec -n "$DRILL_NS" "pod/$VAULT_POD" -- sh -c \
  'VAULT_TOKEN=$(cat /vault/restore-credentials/restore-token); export VAULT_TOKEN; vault kv delete "$1" >/dev/null' -- "$TEST_PATH"
pass "Restored path is readable and secret round-trip succeeded"

section "DRILL SUMMARY"
PASS_COUNT=6
echo "Passed: $PASS_COUNT"
echo "Failed: $FAIL_COUNT"
pass "Vault Raft snapshot restore procedure verified"
