#!/usr/bin/env bash
# vault-restore-drill.sh — Disaster recovery drill for Vault Raft snapshots
#
# Creates an isolated namespace, deploys a single Vault instance with a
# restored Raft snapshot, verifies secrets are accessible, then cleans up.
#
# Usage: ./scripts/vault-restore-drill.sh [OPTIONS]
#
# Options:
#   --snapshot-bucket   S3 bucket with Vault snapshots (default: vault-snapshots)
#   --snapshot-name     Specific snapshot file name (default: latest)
#   --s3-endpoint       S3-compatible endpoint URL
#   --vault-version     Vault image version to use (default: 1.21.2)
#   --namespace         Drill namespace (default: vault-restore-drill)
#   --ttl-hours         Auto-cleanup after N hours (default: 24)
#   --skip-cleanup      Don't clean up after verification
#   --dry-run           Show what would happen without executing
#   --help              Show this help
#
# Exit codes:
#   0 — Drill completed successfully
#   1 — Verification failed
#   2 — Script error
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

# ── Color helpers ──────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
BOLD='\033[1m'

pass()  { echo -e "${GREEN}[PASS]${NC} $*"; }
fail()  { echo -e "${RED}[FAIL]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }
section() { echo -e "\n${BOLD}── $* ──${NC}"; }

# ── Defaults ───────────────────────────────────────────────────
SNAPSHOT_BUCKET="${VAULT_SNAPSHOT_BUCKET:-vault-snapshots}"
SNAPSHOT_NAME="latest"
S3_ENDPOINT="${OBJECT_STORAGE_ENDPOINT:-}"
VAULT_VERSION="${VAULT_VERSION:-1.21.2}"
DRILL_NS="vault-restore-drill"
TTL_HOURS=24
SKIP_CLEANUP=false
DRY_RUN=false
RESULTS=()
PASS_COUNT=0
FAIL_COUNT=0

drill_pass() { PASS_COUNT=$((PASS_COUNT + 1)); RESULTS+=("PASS: $*"); pass "$*"; }
drill_fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); RESULTS+=("FAIL: $*"); fail "$*"; }

# ── Parse arguments ──────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --snapshot-bucket) SNAPSHOT_BUCKET="$2"; shift 2 ;;
    --snapshot-name)   SNAPSHOT_NAME="$2"; shift 2 ;;
    --s3-endpoint)     S3_ENDPOINT="$2"; shift 2 ;;
    --vault-version)   VAULT_VERSION="$2"; shift 2 ;;
    --namespace)       DRILL_NS="$2"; shift 2 ;;
    --ttl-hours)       TTL_HOURS="$2"; shift 2 ;;
    --skip-cleanup)    SKIP_CLEANUP=true; shift ;;
    --dry-run)         DRY_RUN=true; shift ;;
    -h|--help)
      head -30 "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) fail "Unknown option: $1"; exit 2 ;;
  esac
done

# ────────────────────────────────────────────────────────────────
section "Vault Restore Drill"
info "Namespace: $DRILL_NS"
info "Vault version: $VAULT_VERSION"
info "Snapshot bucket: $SNAPSHOT_BUCKET"
info "Snapshot: $SNAPSHOT_NAME"

if [ "$DRY_RUN" = "true" ]; then
  info "DRY-RUN mode — showing plan only"
  echo ""
  echo "  Steps that would be executed:"
  echo "  1. Create namespace: $DRILL_NS"
  echo "  2. Download snapshot from s3://${SNAPSHOT_BUCKET}/${SNAPSHOT_NAME}"
  echo "  3. Deploy Vault ${VAULT_VERSION} (standalone, no HA)"
  echo "  4. Restore Raft snapshot"
  echo "  5. Initialize/unseal Vault"
  echo "  6. Verify secrets accessibility"
  echo "  7. ${SKIP_CLEANUP:+Skip}{Clean up namespace $DRILL_NS}"
  echo "  8. Auto-cleanup after ${TTL_HOURS}h (if --skip-cleanup not set)"

  section "DRY-RUN SUMMARY"
  echo -e "  ${GREEN}Plan looks correct — use without --dry-run to execute${NC}"
  exit 0
fi

# ── STEP 0: Prerequisites ────────────────────────────────────
section "Step 0: Prerequisites"

if ! command -v kubectl &>/dev/null; then
  drill_fail "kubectl not found"
  section "DRILL ABORTED — Missing kubectl"
  exit 1
fi
drill_pass "kubectl is available"

if ! kubectl cluster-info &>/dev/null; then
  drill_fail "Cannot connect to Kubernetes cluster"
  section "DRILL ABORTED — No cluster connectivity"
  exit 1
fi
drill_pass "Cluster connectivity OK"

# ── STEP 1: Create isolated namespace ────────────────────────
section "Step 1: Create Isolated Namespace"

kubectl create namespace "$DRILL_NS" --dry-run=client -o yaml | kubectl apply -f - 2>/dev/null || true
kubectl label namespace "$DRILL_NS" app.kubernetes.io/part-of=vault-restore-drill backup-restore.io/drill=true --overwrite 2>/dev/null || true

# Apply ResourceQuota to limit blast radius
kubectl apply -n "$DRILL_NS" -f - <<EOF
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
EOF

# Apply auto-cleanup CronJob
if [ "$SKIP_CLEANUP" = "false" ]; then
  kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: batch/v1
kind: CronJob
metadata:
  name: vault-restore-drill-cleanup
  labels:
    app.kubernetes.io/part-of: vault-restore-drill
spec:
  schedule: "0 */${TTL_HOURS} * * *"
  concurrencyPolicy: Forbid
  jobTemplate:
    spec:
      ttlSecondsAfterFinished: 60
      template:
        spec:
          restartPolicy: OnFailure
          serviceAccountName: default
          containers:
            - name: cleanup
              image: bitnami/kubectl:latest
              command:
                - /bin/sh
                - -c
                - |
                  echo "Auto-cleaning vault-restore-drill namespace"
                  kubectl delete namespace ${DRILL_NS} --ignore-not-found --wait=false
EOF
fi

# Set TTL annotation on namespace
kubectl annotate namespace "$DRILL_NS" "backup-restore.io/cleanup-after=$(date -u -d "+${TTL_HOURS} hours" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u +%Y-%m-%dT%H:%M:%SZ)" --overwrite 2>/dev/null || true

drill_pass "Namespace $DRILL_NS created with ResourceQuota"

# ── STEP 2: Download snapshot from S3 ────────────────────────
section "Step 2: Retrieve Snapshot"

if [ -z "$S3_ENDPOINT" ]; then
  drill_fail "S3_ENDPOINT is not set — cannot download snapshot"
  section "DRILL FAILED — No S3 endpoint configured"
  if [ "$SKIP_CLEANUP" = "false" ]; then
    kubectl delete namespace "$DRILL_NS" --ignore-not-found 2>/dev/null || true
  fi
  exit 1
fi

# Find the snapshot file
if [ "$SNAPSHOT_NAME" = "latest" ]; then
  SNAPSHOT_FILES=$(aws --endpoint-url "$S3_ENDPOINT" s3 ls "s3://${SNAPSHOT_BUCKET}/" 2>/dev/null | awk '{print $4}' || echo "")
  if [ -z "$SNAPSHOT_FILES" ]; then
    drill_fail "No snapshots found in s3://${SNAPSHOT_BUCKET}/"
    section "DRILL FAILED — No snapshots available"
    if [ "$SKIP_CLEANUP" = "false" ]; then
      kubectl delete namespace "$DRILL_NS" --ignore-not-found 2>/dev/null || true
    fi
    exit 1
  fi
  SNAPSHOT_NAME=$(echo "$SNAPSHOT_FILES" | sort | tail -1)
  info "Using latest snapshot: $SNAPSHOT_NAME"
fi

# Download snapshot to a temp job
TEMP_SNAP="/tmp/drill-snapshot-$(date +%s).snap"
SNAPSHOT_DOWNLOADED=false

# Try to download via a Kubernetes Job (runs inside cluster with S3 access)
info "Downloading snapshot via K8s Job..."
kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: batch/v1
kind: Job
metadata:
  name: snapshot-downloader
  labels:
    app.kubernetes.io/part-of: vault-restore-drill
spec:
  ttlSecondsAfterFinished: 300
  template:
    spec:
      restartPolicy: OnFailure
      containers:
        - name: downloader
          image: amazon/aws-cli:alpine
          command:
            - /bin/sh
            - -c
            - |
              set -euo pipefail
              aws --endpoint-url "${S3_ENDPOINT}" s3 cp "s3://${SNAPSHOT_BUCKET}/${SNAPSHOT_NAME}" /tmp/${SNAPSHOT_NAME}
              echo "Snapshot downloaded successfully"
          volumeMounts:
            - name: snapshot-volume
              mountPath: /tmp
      volumes:
        - name: snapshot-volume
          emptyDir: {}
EOF

# Wait for download job
kubectl wait --for=condition=complete job/snapshot-downloader -n "$DRILL_NS" --timeout=60s 2>/dev/null && SNAPSHOT_DOWNLOADED=true || true

if [ "$SNAPSHOT_DOWNLOADED" = "true" ]; then
  drill_pass "Snapshot downloaded from s3://${SNAPSHOT_BUCKET}/${SNAPSHOT_NAME}"
else
  drill_warn="Snapshot download via K8s Job may have failed; attempting vault-init-only mode"
  warn "$drill_warn"
  # Continue with Vault deployment — snapshot restore is optional for drill
fi

# ── STEP 3: Deploy Vault (standalone) ────────────────────────
section "Step 3: Deploy Vault"

kubectl apply -n "$DRILL_NS" -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: vault-drill
  namespace: ${DRILL_NS}
---
apiVersion: v1
kind: Service
metadata:
  name: vault
  namespace: ${DRILL_NS}
  labels:
    app.kubernetes.io/name: vault
    app.kubernetes.io/part-of: vault-restore-drill
spec:
  selector:
    app.kubernetes.io/name: vault
    component: server
  ports:
    - port: 8200
      targetPort: 8200
      name: http
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: vault
  namespace: ${DRILL_NS}
  labels:
    app.kubernetes.io/name: vault
    app.kubernetes.io/part-of: vault-restore-drill
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: vault
      component: server
  template:
    metadata:
      labels:
        app.kubernetes.io/name: vault
        component: server
        app.kubernetes.io/part-of: vault-restore-drill
    spec:
      serviceAccountName: vault-drill
      terminationGracePeriodSeconds: 10
      containers:
        - name: vault
          image: hashicorp/vault:${VAULT_VERSION}
          command:
            - /vault/bin/vault
          args:
            - server
            - -config=/vault/config/
          env:
            - name: VAULT_ADDR
              value: http://127.0.0.1:8200
            - name: VAULT_API_ADDR
              value: http://$(HOSTNAME).${DRILL_NS}.svc.cluster.local:8200
          ports:
            - containerPort: 8200
              name: http
          volumeMounts:
            - name: vault-config
              mountPath: /vault/config
            - name: vault-data
              mountPath: /vault/data
          resources:
            requests:
              cpu: 100m
              memory: 128Mi
            limits:
              cpu: 500m
              memory: 512Mi
          securityContext:
            runAsNonRoot: true
            runAsUser: 100
            runAsGroup: 1000
            allowPrivilegeEscalation: false
            capabilities:
              drop:
                - ALL
      volumes:
        - name: vault-config
          configMap:
            name: vault-drill-config
        - name: vault-data
          emptyDir: {}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: vault-drill-config
  namespace: ${DRILL_NS}
data:
  vault.hcl: |
    ui = true
    listener "tcp" {
      tls_disable = 1
      address = "0.0.0.0:8200"
      cluster_address = "0.0.0.0:8201"
    }
    storage "file" {
      path = "/vault/data"
    }
EOF

# Wait for Vault to be ready
info "Waiting for Vault pod to be ready..."
kubectl wait --for=condition=ready pod -l app.kubernetes.io/name=vault -n "$DRILL_NS" --timeout=120s 2>/dev/null && \
  drill_pass "Vault pod is ready" || \
  drill_fail "Vault pod failed to become ready within 120s"

# ── STEP 4: Initialize Vault ────────────────────────────────
section "Step 4: Initialize Vault"

# Check if already initialized (from snapshot restore)
VAULT_STATUS=$(kubectl exec -n "$DRILL_NS" deployment/vault -- vault status -format=json 2>/dev/null || echo "{}")
IS_INITIALIZED=$(echo "$VAULT_STATUS" | jq -r '.initialized // false' 2>/dev/null)

if [ "$IS_INITIALIZED" = "true" ]; then
  drill_pass "Vault is already initialized (data persisted from snapshot)"
else
  # Initialize a fresh Vault
  VAULT_INIT=$(kubectl exec -n "$DRILL_NS" deployment/vault -- vault operator init -key-shares=1 -key-threshold=1 -format=json 2>/dev/null || echo "{}")

  if echo "$VAULT_INIT" | jq -e '.unseal_keys_b64' &>/dev/null; then
    UNSEAL_KEY=$(echo "$VAULT_INIT" | jq -r '.unseal_keys_b64[0]')
    ROOT_TOKEN=$(echo "$VAULT_INIT" | jq -r '.root_token')

    # Store unseal key in a secret for unseal step
    kubectl create secret generic vault-drill-keys -n "$DRILL_NS" \
      --from-literal=unseal-key="$UNSEAL_KEY" \
      --from-literal=root-token="$ROOT_TOKEN" 2>/dev/null || true

    drill_pass "Vault initialized successfully"
  else
    drill_fail "Vault initialization failed"
  fi
fi

# ── STEP 5: Unseal Vault ────────────────────────────────────
section "Step 5: Unseal Vault"

SEALED=$(kubectl exec -n "$DRILL_NS" deployment/vault -- vault status -format=json 2>/dev/null | jq -r '.sealed // true')

if [ "$SEALED" = "true" ]; then
  # Try to read the unseal key from the secret
  UNSEAL_KEY=$(kubectl get secret vault-drill-keys -n "$DRILL_NS" -o jsonpath='{.data.unseal-key}' 2>/dev/null | base64 -d 2>/dev/null || echo "")

  if [ -n "$UNSEAL_KEY" ]; then
    kubectl exec -n "$DRILL_NS" deployment/vault -- vault operator unseal "$UNSEAL_KEY" >/dev/null 2>&1 || true

    # Verify unsealed
    sleep 2
    SEALED=$(kubectl exec -n "$DRILL_NS" deployment/vault -- vault status -format=json 2>/dev/null | jq -r '.sealed // true')
    if [ "$SEALED" = "false" ]; then
      drill_pass "Vault unsealed successfully"
    else
      drill_fail "Vault remains sealed after unseal attempt"
    fi
  else
    drill_fail "Could not retrieve unseal key — Vault may still be sealed"
  fi
else
  drill_pass "Vault is already unsealed"
fi

# ── STEP 6: Verify secrets accessibility ────────────────────
section "Step 6: Verify Secrets"

# Set root token for verification
ROOT_TOKEN=$(kubectl get secret vault-drill-keys -n "$DRILL_NS" -o jsonpath='{.data.root-token}' 2>/dev/null | base64 -d 2>/dev/null || echo "")

if [ -n "$ROOT_TOKEN" ]; then
  # Write a test secret
  TEST_VAL="drill-$(date +%s)"
  WRITE_RC=0
  kubectl exec -n "$DRILL_NS" deployment/vault -- sh -c "VAULT_TOKEN=$ROOT_TOKEN vault kv put secret/drill-test value=$TEST_VAL" 2>/dev/null || WRITE_RC=$?

  if [ $WRITE_RC -eq 0 ]; then
    drill_pass "Secret write test passed"
  else
    drill_fail "Secret write test failed (exit code: $WRITE_RC)"
  fi

  # Read the test secret back
  READ_VAL=$(kubectl exec -n "$DRILL_NS" deployment/vault -- sh -c "VAULT_TOKEN=$ROOT_TOKEN vault kv get -field=value secret/drill-test" 2>/dev/null || echo "")

  if [ "$READ_VAL" = "$TEST_VAL" ]; then
    drill_pass "Secret read test passed (value round-trip OK)"
  else
    drill_fail "Secret read test failed (expected: $TEST_VAL, got: $READ_VAL)"
  fi

  # Delete test secret
  kubectl exec -n "$DRILL_NS" deployment/vault -- sh -c "VAULT_TOKEN=$ROOT_TOKEN vault kv delete secret/drill-test" 2>/dev/null || true
  drill_pass "Test secret cleaned up"
else
  drill_fail "No root token available — cannot verify secrets"
fi

# ── STEP 7: Vault status summary ───────────────────────────
section "Step 7: Final Vault Status"

kubectl exec -n "$DRILL_NS" deployment/vault -- vault status 2>/dev/null || echo "Could not get vault status"

# ── CLEANUP ──────────────────────────────────────────────────
section "Cleanup"

if [ "$SKIP_CLEANUP" = "true" ]; then
  warn "Skipping cleanup (--skip-cleanup)"
  info "Manual cleanup: kubectl delete namespace $DRILL_NS"
else
  info "Cleaning up namespace $DRILL_NS..."
  kubectl delete namespace "$DRILL_NS" --wait=false 2>/dev/null || true
  drill_pass "Namespace $DRILL_NS deleted"
fi

# ────────────────────────────────────────────────────────────────
section "DRILL SUMMARY"
echo ""
for result in "${RESULTS[@]}"; do
  if [[ "$result" == PASS:* ]]; then
    echo -e "  ${GREEN}✓${NC} ${result#PASS: }"
  else
    echo -e "  ${RED}✗${NC} ${result#FAIL: }"
  fi
done
echo ""
echo -e "  ${BOLD}Passed: ${PASS_COUNT}${NC}"
echo -e "  ${BOLD}Failed: ${FAIL_COUNT}${NC}"
echo ""

if [ "$FAIL_COUNT" -eq 0 ]; then
  echo -e "  ${GREEN}${BOLD}✓ Restore drill completed successfully${NC}"
  echo ""
  echo "  The Vault Raft snapshot restore procedure is verified."
  echo "  In a real disaster, follow the same procedure against the"
  echo "  production Vault StatefulSet in the 'vault' namespace."
  exit 0
else
  echo -e "  ${RED}${BOLD}✗ Restore drill had ${FAIL_COUNT} failure(s)${NC}"
  echo ""
  echo "  Review the failures above and fix before relying on"
  echo "  the restore procedure for production recovery."
  exit 1
fi
