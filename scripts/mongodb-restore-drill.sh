#!/usr/bin/env bash
# Restore a Percona MongoDB backup into an isolated, single-member test cluster.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/load-project-env.sh
source "${SCRIPT_DIR}/load-project-env.sh"

SOURCE_NAMESPACE="databases"
SOURCE_CLUSTER="${PROJECT_NAME:-k8s}-mongo"
BACKUP_REF=""
BACKUP_TYPE=""
STORAGE_NAME="s3-object-storage"
DRILL_NAMESPACE="mongodb-restore-drill"
TARGET_CLUSTER="mongodb-drill"
OPERATOR_VERSION="1.22.0"
VERIFY_DATABASE=""
VERIFY_COLLECTION=""
MIN_DOCUMENTS=1
TIMEOUT_SECONDS=3600
TTL_HOURS=24
STORAGE_SIZE="20Gi"
SKIP_CLEANUP=false
DRY_RUN=false
DRILL_CREATED=false
DRILL_SUCCEEDED=false

usage() {
  cat <<'EOF'
Usage: mongodb-restore-drill.sh --backup <backup-name|s3-uri> [options]

Required:
  --backup REF                PerconaServerMongoDBBackup name, or an S3 URI

Options:
  --backup-type TYPE          Required for an S3 URI (logical|physical|incremental)
  --source-namespace NS       Source namespace (default: databases)
  --source-cluster NAME       Source cluster (default: $PROJECT_NAME-mongo or k8s-mongo)
  --storage-name NAME         Source backup storage (default: s3-object-storage)
  --namespace NS              Isolated drill namespace (default: mongodb-restore-drill)
  --target-cluster NAME       Disposable cluster name (default: mongodb-drill)
  --operator-version VERSION  PSMDB Operator/chart version (default: 1.22.0)
  --verify-database NAME      Database containing a recovery sentinel
  --verify-collection NAME    Collection containing a recovery sentinel
  --min-documents COUNT       Minimum sentinel documents (default: 1)
  --timeout-seconds SECONDS   Restore timeout (default: 3600)
  --ttl-hours HOURS           Expiry label for external janitors (default: 24)
  --storage-size SIZE         Disposable MongoDB PVC size (default: 20Gi)
  --skip-cleanup              Preserve the namespace after a successful drill
  --dry-run                   Print the exact plan without changing the cluster
  -h, --help                  Show this help

The source cluster is never modified. The script installs a namespace-scoped
operator, restores into a one-member disposable cluster, verifies MongoDB
connectivity and optional sentinel data, and deletes the drill namespace after
success unless --skip-cleanup is set.
EOF
}

log() { printf '[%s] %s\n' "$1" "$2"; }
die() { log ERROR "$*" >&2; exit 1; }

cleanup() {
  local rc=$?
  if [[ "$DRILL_CREATED" == true && "$SKIP_CLEANUP" == false && "$DRILL_SUCCEEDED" == true ]]; then
    log INFO "Finalizing the disposable MongoDB cluster before deleting ${DRILL_NAMESPACE}"
    kubectl delete perconaservermongodbrestore --all --namespace "$DRILL_NAMESPACE" \
      --wait=true --timeout=5m >/dev/null 2>&1 || true
    kubectl delete perconaservermongodb "$TARGET_CLUSTER" --namespace "$DRILL_NAMESPACE" \
      --wait=true --timeout=10m >/dev/null 2>&1 || true
    helm uninstall mongodb-restore-drill-operator --namespace "$DRILL_NAMESPACE" \
      --wait --timeout 5m >/dev/null 2>&1 || true
    kubectl delete namespace "$DRILL_NAMESPACE" --wait=true --timeout=10m \
      >/dev/null 2>&1 || true
  elif [[ "$DRILL_CREATED" == true && "$rc" -ne 0 ]]; then
    log WARN "Drill failed; preserving ${DRILL_NAMESPACE} for diagnosis (expires-after=${TTL_HOURS}h)"
  fi
}
trap cleanup EXIT

while [[ $# -gt 0 ]]; do
  case "$1" in
    --backup) BACKUP_REF="${2:?missing backup reference}"; shift 2 ;;
    --backup-type) BACKUP_TYPE="${2:?missing backup type}"; shift 2 ;;
    --source-namespace) SOURCE_NAMESPACE="${2:?missing source namespace}"; shift 2 ;;
    --source-cluster) SOURCE_CLUSTER="${2:?missing source cluster}"; shift 2 ;;
    --storage-name) STORAGE_NAME="${2:?missing storage name}"; shift 2 ;;
    --namespace) DRILL_NAMESPACE="${2:?missing namespace}"; shift 2 ;;
    --target-cluster) TARGET_CLUSTER="${2:?missing target cluster}"; shift 2 ;;
    --operator-version) OPERATOR_VERSION="${2:?missing operator version}"; shift 2 ;;
    --verify-database) VERIFY_DATABASE="${2:?missing database}"; shift 2 ;;
    --verify-collection) VERIFY_COLLECTION="${2:?missing collection}"; shift 2 ;;
    --min-documents) MIN_DOCUMENTS="${2:?missing count}"; shift 2 ;;
    --timeout-seconds) TIMEOUT_SECONDS="${2:?missing timeout}"; shift 2 ;;
    --ttl-hours) TTL_HOURS="${2:?missing TTL}"; shift 2 ;;
    --storage-size) STORAGE_SIZE="${2:?missing storage size}"; shift 2 ;;
    --skip-cleanup) SKIP_CLEANUP=true; shift ;;
    --dry-run) DRY_RUN=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) log ERROR "Unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$BACKUP_REF" ]] || { log ERROR "Missing --backup" >&2; usage >&2; exit 2; }
[[ "$MIN_DOCUMENTS" =~ ^[0-9]+$ ]] || die "--min-documents must be a non-negative integer"
[[ "$TIMEOUT_SECONDS" =~ ^[1-9][0-9]*$ ]] || die "--timeout-seconds must be positive"
[[ "$TTL_HOURS" =~ ^[1-9][0-9]*$ ]] || die "--ttl-hours must be positive"
[[ "$STORAGE_SIZE" =~ ^[1-9][0-9]*(Mi|Gi|Ti)$ ]] \
  || die "--storage-size must be a positive Kubernetes binary quantity"
[[ -z "$VERIFY_COLLECTION" || -n "$VERIFY_DATABASE" ]] || die "--verify-collection requires --verify-database"
[[ "$VERIFY_DATABASE" =~ ^[A-Za-z0-9_.-]*$ ]] || die "Unsafe database name"
[[ "$VERIFY_COLLECTION" =~ ^[A-Za-z0-9_.-]*$ ]] || die "Unsafe collection name"
[[ "$DRILL_NAMESPACE" != "$SOURCE_NAMESPACE" ]] || die "Drill namespace must differ from source namespace"
[[ "$TARGET_CLUSTER" != "$SOURCE_CLUSTER" ]] || die "Target cluster must differ from source cluster"

if [[ "$BACKUP_REF" == *://* ]]; then
  case "$BACKUP_TYPE" in
    logical|physical|incremental) ;;
    "") die "--backup-type is required when --backup is an S3 URI" ;;
    *) die "Unsupported --backup-type: ${BACKUP_TYPE}" ;;
  esac
fi

if [[ "$DRY_RUN" == true ]]; then
  cat <<EOF
MONGODB RESTORE DRILL PLAN
Source:          ${SOURCE_NAMESPACE}/${SOURCE_CLUSTER}
Backup:          ${BACKUP_REF}
Storage:         ${STORAGE_NAME}
Target:          ${DRILL_NAMESPACE}/${TARGET_CLUSTER}
Operator:        ${OPERATOR_VERSION}
Expiry label:    ${TTL_HOURS}h
Cleanup:         $([[ "$SKIP_CLEANUP" == true ]] && printf 'preserve' || printf 'delete after success')

1. Verify source cluster and completed backup metadata.
2. Create a restricted, isolated namespace and quota.
3. Copy only the source user and object-storage credential secrets.
4. Install a namespace-scoped Percona MongoDB Operator.
5. Create a one-member, non-sharded disposable cluster with backups and PMM disabled.
6. Restore with a PerconaServerMongoDBRestore backupSource contract.
7. Verify operator state, mongosh connectivity, and optional sentinel data.
8. Delete the successful drill namespace unless --skip-cleanup was requested.
EOF
  exit 0
fi

for command_name in kubectl helm jq; do
  command -v "$command_name" >/dev/null 2>&1 || die "Required command not found: ${command_name}"
done
kubectl cluster-info >/dev/null 2>&1 || die "Kubernetes cluster is unreachable"
kubectl get crd perconaservermongodbs.psmdb.percona.com >/dev/null 2>&1 \
  || die "Percona MongoDB CRD is not installed"

SOURCE_JSON=$(kubectl get perconaservermongodb "$SOURCE_CLUSTER" \
  --namespace "$SOURCE_NAMESPACE" -o json) \
  || die "Source MongoDB cluster ${SOURCE_NAMESPACE}/${SOURCE_CLUSTER} was not found"
SOURCE_STATE=$(jq -r '.status.state // "unknown" | ascii_downcase' <<<"$SOURCE_JSON")
[[ "$SOURCE_STATE" == ready ]] || die "Source MongoDB cluster is not ready (state=${SOURCE_STATE})"

STORAGE_JSON=$(jq -c --arg storage "$STORAGE_NAME" '.spec.backup.storages[$storage] // empty' \
  <<<"$SOURCE_JSON")
[[ -n "$STORAGE_JSON" ]] || die "Backup storage ${STORAGE_NAME} is absent from the source cluster"
CREDENTIAL_SECRET=$(jq -r '.s3.credentialsSecret // .azure.credentialsSecret // .gcs.credentialsSecret // empty' \
  <<<"$STORAGE_JSON")
[[ -n "$CREDENTIAL_SECRET" ]] || die "Backup storage ${STORAGE_NAME} has no credential secret"

if kubectl get perconaservermongodbbackup "$BACKUP_REF" --namespace "$SOURCE_NAMESPACE" >/dev/null 2>&1; then
  BACKUP_JSON=$(kubectl get perconaservermongodbbackup "$BACKUP_REF" \
    --namespace "$SOURCE_NAMESPACE" -o json)
  BACKUP_STATE=$(jq -r '.status.state // "unknown" | ascii_downcase' <<<"$BACKUP_JSON")
  case "$BACKUP_STATE" in
    ready|succeeded) ;;
    *) die "Backup ${BACKUP_REF} is not restorable (state=${BACKUP_STATE})" ;;
  esac
  BACKUP_SOURCE=$(jq -cn \
    --argjson status "$(jq -c '.status' <<<"$BACKUP_JSON")" \
    --argjson storage "$STORAGE_JSON" \
    --arg credential "$CREDENTIAL_SECRET" '
      ($status | {
        destination,
        type,
        storageName,
        s3,
        azure,
        gcs
      } | with_entries(select(.value != null))) as $source
      | if ($source.destination // "") == "" then error("backup has no destination") else . end
      | $source
      | if has("s3") then .s3.credentialsSecret = $credential
        elif has("azure") then .azure.credentialsSecret = $credential
        elif has("gcs") then .gcs.credentialsSecret = $credential
        elif ($storage.type // "") == "s3" then .s3 = ($storage.s3 + {credentialsSecret: $credential})
        elif ($storage.type // "") == "azure" then .azure = ($storage.azure + {credentialsSecret: $credential})
        elif ($storage.type // "") == "gcs" then .gcs = ($storage.gcs + {credentialsSecret: $credential})
        else error("unsupported backup storage") end') \
    || die "Could not construct backupSource from ${BACKUP_REF}"
else
  [[ "$BACKUP_REF" == *://* ]] || die "Backup CR ${BACKUP_REF} was not found; use an S3 URI for off-cluster metadata"
  [[ $(jq -r '.type // empty' <<<"$STORAGE_JSON") == s3 ]] \
    || die "Direct URI restore currently requires S3-compatible storage"
  BACKUP_SOURCE=$(jq -cn --arg destination "$BACKUP_REF" --arg type "$BACKUP_TYPE" \
    --arg credential "$CREDENTIAL_SECRET" --arg storage_name "$STORAGE_NAME" \
    --argjson storage "$STORAGE_JSON" '{
      destination: $destination,
      type: $type,
      storageName: $storage_name,
      s3: ($storage.s3 + {credentialsSecret: $credential})
    }')
fi

SOURCE_USERS_SECRET="internal-${SOURCE_CLUSTER}-users"
if ! kubectl get secret "$SOURCE_USERS_SECRET" --namespace "$SOURCE_NAMESPACE" >/dev/null 2>&1; then
  SOURCE_USERS_SECRET=$(jq -r --arg fallback "$SOURCE_USERS_SECRET" \
    '.spec.secrets.users // $fallback' <<<"$SOURCE_JSON")
fi
TARGET_USERS_SECRET="internal-${TARGET_CLUSTER}-users"
kubectl get secret "$SOURCE_USERS_SECRET" --namespace "$SOURCE_NAMESPACE" >/dev/null 2>&1 \
  || die "Source users secret ${SOURCE_USERS_SECRET} was not found"
kubectl get secret "$CREDENTIAL_SECRET" --namespace "$SOURCE_NAMESPACE" >/dev/null 2>&1 \
  || die "Backup credential secret ${CREDENTIAL_SECRET} was not found"

if kubectl get namespace "$DRILL_NAMESPACE" >/dev/null 2>&1; then
  die "Drill namespace ${DRILL_NAMESPACE} already exists; inspect or delete it explicitly"
fi

EXPIRES_AT=$(date -u -v+"${TTL_HOURS}"H '+%Y-%m-%dT%H:%M:%SZ' 2>/dev/null \
  || date -u -d "+${TTL_HOURS} hours" '+%Y-%m-%dT%H:%M:%SZ')
kubectl create namespace "$DRILL_NAMESPACE" >/dev/null
DRILL_CREATED=true
kubectl label namespace "$DRILL_NAMESPACE" \
  pod-security.kubernetes.io/enforce=baseline \
  backup-restore.io/drill=true --overwrite >/dev/null
kubectl annotate namespace "$DRILL_NAMESPACE" \
  backup-restore.io/expires-at="$EXPIRES_AT" --overwrite >/dev/null
kubectl apply --namespace "$DRILL_NAMESPACE" -f - >/dev/null <<EOF
apiVersion: v1
kind: ResourceQuota
metadata:
  name: mongodb-drill-quota
spec:
  hard:
    pods: "12"
    persistentvolumeclaims: "4"
    requests.storage: 100Gi
EOF

copy_secret() {
  local source_name=$1 target_name=$2
  kubectl get secret "$source_name" --namespace "$SOURCE_NAMESPACE" -o json \
    | jq --arg name "$target_name" --arg namespace "$DRILL_NAMESPACE" '
        del(.metadata.annotations,.metadata.creationTimestamp,.metadata.finalizers,
            .metadata.generateName,.metadata.generation,.metadata.labels,
            .metadata.managedFields,.metadata.ownerReferences,.metadata.resourceVersion,
            .metadata.selfLink,.metadata.uid)
        | .metadata.name = $name
        | .metadata.namespace = $namespace' \
    | kubectl apply --namespace "$DRILL_NAMESPACE" -f - >/dev/null
}
copy_secret "$SOURCE_USERS_SECRET" "$TARGET_USERS_SECRET"
copy_secret "$CREDENTIAL_SECRET" "$CREDENTIAL_SECRET"

helm repo add percona https://percona.github.io/percona-helm-charts --force-update >/dev/null
helm repo update percona >/dev/null
helm upgrade --install mongodb-restore-drill-operator percona/psmdb-operator \
  --namespace "$DRILL_NAMESPACE" \
  --version "$OPERATOR_VERSION" \
  --set watchNamespace="$DRILL_NAMESPACE" \
  --set watchAllNamespaces=false \
  --set disableTelemetry=true \
  --set resources.requests.cpu=50m \
  --set resources.requests.memory=64Mi \
  --set resources.limits.cpu=250m \
  --set resources.limits.memory=256Mi \
  --wait --timeout 10m >/dev/null

TARGET_JSON=$(jq --arg name "$TARGET_CLUSTER" --arg namespace "$DRILL_NAMESPACE" \
  --arg users_secret "$TARGET_USERS_SECRET" --arg storage_size "$STORAGE_SIZE" '
    del(.metadata.annotations,.metadata.creationTimestamp,.metadata.finalizers,
        .metadata.generateName,.metadata.generation,.metadata.labels,
        .metadata.managedFields,.metadata.ownerReferences,.metadata.resourceVersion,
        .metadata.selfLink,.metadata.uid,.status)
    | .metadata.name = $name
    | .metadata.namespace = $namespace
    | .spec.replsets |= map(
        .size = 1
        | .volumeSpec.persistentVolumeClaim.resources.requests.storage = $storage_size
        | .tolerations = [])
    | .spec.sharding.enabled = false
    | del(.spec.sharding.mongos, .spec.sharding.configsvrReplSet)
    | .spec.unsafeFlags.replsetSize = true
    | .spec.unsafeFlags.mongosSize = true
    | .spec.secrets.users = $users_secret
    # The restore controller reads the target backup-agent version before it
    # starts PBM. Keep the agent enabled, but remove schedules and PITR so the
    # disposable cluster cannot create independent backups.
    | .spec.backup.enabled = true
    | .spec.backup.tasks = []
    | .spec.backup.pitr.enabled = false
    | .spec.pmm.enabled = false' <<<"$SOURCE_JSON")
kubectl apply --namespace "$DRILL_NAMESPACE" -f - >/dev/null <<<"$TARGET_JSON"

wait_for_state() {
  local resource=$1 name=$2 desired=$3 deadline=$((SECONDS + TIMEOUT_SECONDS)) state
  while (( SECONDS < deadline )); do
    state=$(kubectl get "$resource" "$name" --namespace "$DRILL_NAMESPACE" \
      -o jsonpath='{.status.state}' 2>/dev/null || true)
    state=$(printf '%s' "$state" | tr '[:upper:]' '[:lower:]')
    if [[ "$state" == "$desired" ]]; then
      return 0
    fi
    case "$state" in
      error|failed) return 1 ;;
    esac
    sleep 10
  done
  return 1
}

log INFO "Waiting for disposable MongoDB cluster to become ready"
wait_for_state perconaservermongodb "$TARGET_CLUSTER" ready \
  || die "Disposable cluster did not become ready within ${TIMEOUT_SECONDS}s"

RESTORE_NAME="restore-$(date -u +%Y%m%d%H%M%S)"
RESTORE_JSON=$(jq -cn --arg name "$RESTORE_NAME" --arg namespace "$DRILL_NAMESPACE" \
  --arg cluster "$TARGET_CLUSTER" --argjson source "$BACKUP_SOURCE" '{
    apiVersion: "psmdb.percona.com/v1",
    kind: "PerconaServerMongoDBRestore",
    metadata: {name: $name, namespace: $namespace},
    spec: {clusterName: $cluster, backupSource: $source}
  }')
kubectl apply --namespace "$DRILL_NAMESPACE" -f - >/dev/null <<<"$RESTORE_JSON"
log INFO "Waiting for ${RESTORE_NAME} to finish"
if ! wait_for_state perconaservermongodbrestore "$RESTORE_NAME" ready; then
  RESTORE_STATUS=$(kubectl get perconaservermongodbrestore "$RESTORE_NAME" \
    --namespace "$DRILL_NAMESPACE" -o json 2>/dev/null | jq -c '.status' || true)
  die "MongoDB restore failed or timed out: ${RESTORE_STATUS:-no status}"
fi

wait_for_state perconaservermongodb "$TARGET_CLUSTER" ready \
  || die "Restored cluster is not ready"
MONGO_POD=$(kubectl get pods --namespace "$DRILL_NAMESPACE" \
  -l "app.kubernetes.io/instance=${TARGET_CLUSTER},app.kubernetes.io/component=mongod" \
  -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)
if [[ -z "$MONGO_POD" ]]; then
  MONGO_POD=$(kubectl get pods --namespace "$DRILL_NAMESPACE" -o json \
    | jq -r --arg prefix "${TARGET_CLUSTER}-rs0-" \
      '.items[] | select(.metadata.name | startswith($prefix)) | .metadata.name' | head -1)
fi
[[ -n "$MONGO_POD" ]] || die "Restored mongod pod was not found"

MONGO_USER=$(kubectl get secret "$TARGET_USERS_SECRET" --namespace "$DRILL_NAMESPACE" \
  -o jsonpath='{.data.MONGODB_DATABASE_ADMIN_USER}' | base64 --decode)
MONGO_PASSWORD=$(kubectl get secret "$TARGET_USERS_SECRET" --namespace "$DRILL_NAMESPACE" \
  -o jsonpath='{.data.MONGODB_DATABASE_ADMIN_PASSWORD}' | base64 --decode)
[[ -n "$MONGO_USER" && -n "$MONGO_PASSWORD" ]] || die "Restored database-admin credentials are missing"

PING_RESULT=$(kubectl exec --namespace "$DRILL_NAMESPACE" "$MONGO_POD" -c mongod -- \
  mongosh --quiet --username "$MONGO_USER" --password "$MONGO_PASSWORD" \
  --authenticationDatabase admin --eval 'db.adminCommand({ping: 1}).ok' 2>/dev/null)
[[ "$PING_RESULT" == *1* ]] || die "mongosh ping failed after restore"

if [[ -n "$VERIFY_DATABASE" ]]; then
  if [[ -n "$VERIFY_COLLECTION" ]]; then
    VERIFY_JS="db.getSiblingDB('${VERIFY_DATABASE}').getCollection('${VERIFY_COLLECTION}').countDocuments({})"
  else
    VERIFY_JS="db.adminCommand({listDatabases: 1}).databases.some(d => d.name === '${VERIFY_DATABASE}') ? 1 : 0"
  fi
  VERIFY_RESULT=$(kubectl exec --namespace "$DRILL_NAMESPACE" "$MONGO_POD" -c mongod -- \
    mongosh --quiet --username "$MONGO_USER" --password "$MONGO_PASSWORD" \
    --authenticationDatabase admin --eval "$VERIFY_JS" 2>/dev/null | tail -1)
  [[ "$VERIFY_RESULT" =~ ^[0-9]+$ ]] || die "Sentinel query did not return an integer: ${VERIFY_RESULT}"
  (( VERIFY_RESULT >= MIN_DOCUMENTS )) \
    || die "Sentinel verification failed: ${VERIFY_RESULT} < ${MIN_DOCUMENTS}"
fi

DRILL_SUCCEEDED=true
log PASS "MongoDB backup ${BACKUP_REF} restored and verified in ${DRILL_NAMESPACE}/${TARGET_CLUSTER}"
