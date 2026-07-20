# Backup and Restore

The platform has three complementary recovery layers. A production recovery is
complete only when all three succeed:

1. Application-native backups for transactionally consistent databases and
   products with their own restore contract.
2. Velero resource and Kopia filesystem backups for Kubernetes objects and
   every mounted PVC, written to storage outside the protected cluster.
3. An encrypted workstation-side cluster bundle containing etcd,
   control-plane PKI, generated secrets, desired configuration, Helm state,
   Kubespray inventory, and Hetzner state.

## Components covered

| Component | Backup mechanism | Scheduled/on-demand resource | Namespace |
|---|---|---|---|
| MongoDB | Percona/PBM backup | operator task / `PerconaServerMongoDBBackup` | `databases` |
| PostgreSQL | Percona Operator pgBackRest | operator schedule / `PerconaPGBackup` | `databases` |
| Vault | Raft snapshot to S3 | `vault-raft-snapshot` | `vault` |
| SeaweedFS | topology check plus Velero/Kopia filesystem copy | `seaweedfs-backup-check` / `full-cluster` | `storage` / `velero` |
| GitLab | chart Toolbox `backup-utility` | `gitlab-toolbox-backup` | `gitlab` |
| GitLab encryption keys | Rails `secrets.yml` copy to S3 | `gitlab-rails-secrets-backup` | `gitlab` |
| All Kubernetes resources | Velero backup | `full-cluster` schedule | `velero` |
| Every mounted PVC | Velero node-agent/Kopia filesystem backup | `full-cluster` schedule | `velero` |
| Kubernetes control plane | etcd snapshot + PKI bundle | `cluster-backup.sh` | external workstation |

Only supported, enabled platform components receive backup resources and
verification requirements. Credentials are generated secrets; insecure static
fallbacks are rejected.

Coroot/ClickHouse, VictoriaMetrics/Loki, Argo CD, Temporal, Postal,
Elasticsearch, Dragonfly, GlitchTip, Daytona, and SeaweedFS data are protected
by Velero filesystem backup rather than an application-aware export. This is a
recoverable filesystem copy, but it is not transactionally equivalent to the
PostgreSQL, MongoDB, Vault, or GitLab native mechanisms. Keep the native and
filesystem layers together. A component's guarded `--delete-data` flag is not
evidence that a backup exists.

Loki PVC retention is explicitly `Retain` for StatefulSet scale and deletion.
If a restore is required, include the source Pod and PVC in the Velero restore
so the node-agent can inject the filesystem replay. For a complete SeaweedFS
cutover, restore the master, filer, volume data, and index claims together on a
replacement cluster. The automated drill deliberately restores one selected
claim into a dynamically provisioned, network-isolated PVC, proves the Kopia
snapshot ID and byte count, and then removes it. It never registers restored
volumes with the live master quorum.

## External disaster-recovery storage

`medium`, `medium-optimized`, and `production` enable the external DR layer.
Before deploying one of those profiles, configure:

```yaml
backup:
  disaster_recovery:
    enabled: true
    endpoint: https://s3.example-provider.com
    region: us-east-1
    bucket: company-platform-dr
    prefix: k8s/velero
    schedule: "30 2 * * *"
    retention_hours: 720
```

Store independent credentials in the gitignored, mode-`0600` project `.env`:

```dotenv
BACKUP_DR_ACCESS_KEY=...
BACKUP_DR_SECRET_KEY=...
```

Backup, restore, orchestration, and migration commands load `.env`
automatically. Explicitly exported variables still take precedence.

The role rejects `.svc` and SeaweedFS endpoints. Backing up a cluster into the
same cluster is not disaster recovery. Use object lock/versioning and a
separate failure domain where the provider supports them.

## Quick Start

Install/update the backup resources through the main playbook, then trigger
the configured CronJobs:

```bash
ansible-playbook -i inventory.yml playbooks/deploy_platform.yml --tags backup
./scripts/backup-all.sh --dry-run
./scripts/backup-all.sh --config platform-orchestrator/platform.yaml --force

# Complete encrypted cluster backup. Never pass a passphrase on argv.
export CLUSTER_BACKUP_PASSPHRASE='use-a-secret-manager-value'
./platform-orchestrator/platform.sh backup-cluster --force

# Or use an age recipient instead of a shared passphrase.
./platform-orchestrator/platform.sh backup-cluster \
  --recipient age1example... --output-dir /secure/offsite/path --force
```

`backup-all.sh` triggers an on-demand full `PerconaPGBackup`, a PBM MongoDB
backup, Vault snapshot, SeaweedFS topology artifact, GitLab archive, and GitLab
Rails secrets for the components enabled by the supplied platform config. An
enabled component that is missing or unhealthy is a backup failure, while a
disabled component is skipped. `cluster-backup.sh` then
requires a completed Velero resource/PVC backup and captures the control-plane
and cloud recovery bundle. Missing layers fail closed; skip options require the
explicit `--allow-incomplete` marker, and such a bundle cannot be restored by
`cluster-restore.sh`.

Before creating the Velero object, the script records every PVC-backed volume
mounted by every pod. It then requires a completed `PodVolumeBackup` for every
recorded namespace/pod/volume tuple and rejects partial or missing filesystem
copies. PVCs that are not mounted are not claimed as protected data; either
mount and back them up through an application-aware process or remove obsolete
claims after verifying retention requirements.

The encrypted archive has an external checksum sidecar and an internal
`SHA256SUMS` manifest. Temporary plaintext is created with mode `0700`/`0600`
and deleted on every exit path. The bundle contains Kubernetes Secrets and PKI;
store the decryption identity separately.

## Profile migration backup gates

Every named-profile transition, including a downgrade to `minimal` or `small`,
temporarily bootstraps the same independent Velero target and requires a
complete encrypted cluster bundle before topology or service changes. A second
bundle is required after target reconciliation. `migrate finalize` will not
retire services, PVCs, or nodes without that post-migration checkpoint; it then
takes a third recovery point after scale-in and before removing Velero when the
target profile disables scheduled backup.

The migration state stores backup identifiers and generated configs, never the
age identity or passphrase. External backup objects and Loki archive buckets
are not deleted by finalization. Existing retained PVCs are not shrunk in
place; larger safe requests are recorded in `storage-retention.tsv`. SeaweedFS,
Vault Raft, and same-topology VMCluster replica reductions are likewise retained
in `stateful-retention.tsv` instead of risking quorum or shard loss. Obsolete
source-topology PVCs are deleted only after backup and explicit `FINALIZE`
confirmation.

## Configuration

```yaml
backup:
  enabled: true
  schedule: "0 2 * * *"
  retention_days: 30
```

Resolved defaults are in `roles/backup-restore/defaults/main.yml`. The S3
bucket is `backups`; GitLab Toolbox archives use the chart's
`gitlab-backups` bucket. The verification job checks only enabled components.

## Restore drills

Always start in dry-run mode and use an isolated test cluster/namespace:

```bash
./scripts/pg-restore-drill.sh --dry-run
./scripts/restore-drill.sh --component mongodb --backup BACKUP_CR --dry-run
./scripts/vault-restore-drill.sh --dry-run
./scripts/restore-drill.sh --component seaweedfs --backup VELERO_BACKUP --dry-run
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID

# Verify encryption and both checksum layers without a cluster mutation.
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.enc --mode verify

# Restore resources and PVC data into a replacement cluster context.
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.enc \
  --mode velero --confirm RESTORE_k8s
```

The generic `restore-drill.sh` dispatcher supports PostgreSQL, MongoDB, Vault,
SeaweedFS, and GitLab.
The MongoDB path deploys a namespace-scoped Percona operator and disposable
single-member cluster, applies `PerconaServerMongoDBRestore`, then verifies
operator state, `mongosh` connectivity, and an optional database/collection
sentinel. PostgreSQL uses its dedicated isolated pgBackRest drill. The
SeaweedFS path selects exactly one completed PodVolumeBackup, pre-creates a
fresh PVC in an isolated namespace, waits for Velero's Kopia restore helper,
matches the restored snapshot ID and byte count, verifies the data read-only,
and cleans up. It proves a filesystem recovery primitive; a full cutover still
requires all related claims on a replacement cluster.

For a meaningful MongoDB integrity check, identify a stable sentinel:

```bash
./scripts/restore-drill.sh --component mongodb --backup mongodb-backup-20260716 \
  --namespace mongodb-drill-20260716 --force

./scripts/mongodb-restore-drill.sh --backup mongodb-backup-20260716 \
  --verify-database recovery_sentinels \
  --verify-collection backup_markers --min-documents 1
```

An actual Vault drill also requires the original snapshot unseal material and
a known path whose restored value must be readable:

```bash
export OBJECT_STORAGE_ENDPOINT=https://s3.example.internal
export VAULT_RESTORE_VERIFY_PATH='secret/known-recovery-sentinel'
./scripts/vault-restore-drill.sh \
  --snapshot-name vault-20260716T020000Z.snap \
  --credentials-secret vault-restore-drill-credentials
```

The source credentials Secret must contain `restore-token` and all original
unseal shares needed to reach the threshold as newline-delimited
`restore-unseal-keys`. The drill restores the snapshot, writes the documented
single-peer `peers.json` recovery file for the isolated copy, unseals with the
required number of shares, verifies one active voting leader and the sentinel,
then cleans up. `VAULT_RESTORE_UNSEAL_KEY` remains a legacy convenience only
for a one-share Vault.

The GitLab artifact drill downloads the selected archive directly into an
isolated PVC, verifies its remote and local sizes, safely extracts it with the
source release's exact Toolbox image, and verifies metadata and repository
payload. The platform configures Toolbox with `--skip db` because GitLab uses
external Percona PostgreSQL; the matching pgBackRest restore drill is a
separate mandatory gate rather than a database dump inside the archive.
For a disaster recovery cutover, restore a same-version GitLab chart with the
official Toolbox `backup-utility --restore`, restore the saved Rails secret,
and follow the GitLab restore runbook. A Helm rollback is not a data restore.

### Replacement-cluster restore

1. Provision the target Kubernetes foundation with the same pinned versions.
2. Configure the target Velero installation read/write against the external
   bucket and wait for the source backup to synchronize.
3. Switch `kubectl` to the replacement cluster. The restore script refuses the
   source context recorded in the bundle.
4. Run `cluster-restore.sh --mode velero` with the exact confirmation.
5. Restore/test the application-native artifacts and original Vault/GitLab key
   material where required.
6. Run health gates and application checks before changing DNS.

### Lost-quorum etcd recovery

The `etcd` mode is only for a Kubespray cluster with at least one surviving
control-plane node and an unhealthy Kubernetes API. It refuses a healthy API
and invokes Kubespray `v2.31.0` `recover-control-plane.yml` with the verified
snapshot:

```bash
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.enc \
  --mode etcd \
  --inventory /secure/updated-replacement-hosts.yml \
  --survivor k8s-master-1 \
  --broken-node k8s-master-2 \
  --broken-node k8s-master-3 \
  --confirm RESTORE_ETCD_k8s
```

Omit `--inventory` only when the surviving/replacement nodes still use the
addresses recorded in the bundle. The recovery script validates every node as
both an etcd and control-plane member, puts the survivor first in both groups,
and records each broken node's original etcd member name.

For total control-plane loss, build a replacement cluster and use the Velero
path. Do not improvise a multi-member etcd restore while API servers are live.

## Safety Gates

1. The selected artifact must exist and be non-empty.
2. Restore namespaces must be isolated from production and carry resource
   limits. Successful scripts delete the namespace unless preservation is
   explicitly requested.
3. Production database clients must never point at the drill namespace.
4. The drill exits nonzero on failed import or payload checks.
5. Rails secrets, object storage, databases, and repository data are all
   separate recovery dependencies.
6. The external Velero target must not be the protected SeaweedFS cluster.
7. A SeaweedFS filesystem drill must match exactly one completed
   PodVolumeBackup/PodVolumeRestore pair and must not join a live quorum.
8. Record artifact ID, source version, size, checks, and cleanup outcome.

## Ongoing verification

The `backup-verification` CronJob checks application artifacts daily. Velero
validates the external `BackupStorageLocation` and records each resource/PVC
backup phase. These are freshness gates, not proof of restorability. Schedule a
replacement-cluster restore drill and the isolated PostgreSQL, Vault, GitLab,
MongoDB, and SeaweedFS drills at the required RPO/RTO cadence. A backup is not
accepted as production-ready until its restore drill has succeeded.
