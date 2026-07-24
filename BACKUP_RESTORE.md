# Backup and Restore

The platform has three complementary recovery layers. A production recovery is
complete only when all three succeed:

1. Application-native backups for transactionally consistent databases and
   products with their own restore contract.
2. Velero resource and Kopia filesystem backups for Kubernetes objects and
   every mounted PVC, written to storage outside the protected cluster.
3. An encrypted workstation-side cluster bundle containing etcd,
   control-plane PKI, generated secrets, the already-Ansible-Vault-encrypted
   Vault initialization/unseal material, desired configuration, Helm state,
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

Coroot/ClickHouse, VictoriaMetrics/Loki, Argo CD, Elasticsearch, Dragonfly, and
SeaweedFS data are protected
by Velero filesystem backup rather than an application-aware export. This is a
recoverable filesystem copy, but it is not transactionally equivalent to the
PostgreSQL, MongoDB, Vault, or GitLab native mechanisms. Keep the native and
filesystem layers together. A component's guarded `--delete-data` flag is not
evidence that a backup exists.

When explicitly enabled, Temporal, Postal, GlitchTip, and Daytona data also
enter the Velero filesystem layer. They are not present in any named profile's
base backup inventory.

`medium-optimized` places only application-replicated claims on the
`platform-local` StorageClass. Those PVs are retained and node-affine: Kubernetes
cannot attach them to a replacement node. Loss of one member is repaired by
SeaweedFS, Vault Raft, PostgreSQL, MongoDB, or Elasticsearch quorum; loss of
the cluster or quorum is recovered from the independent native and
Velero/Kopia layers above. Singleton, audit, repository, UI-state, and staging
claims remain on `hcloud-volumes`.

StorageClass is immutable. Converting an existing CSI-backed cluster to the
hybrid layout requires a completed external backup, a newly provisioned target
using the desired profile, and target-bound native/Velero restore. The
in-place profile migrator reports the exact class-transition map and refuses
to mutate those claims.

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
BACKUP_DR_ENDPOINT=https://s3.example-provider.com
BACKUP_DR_BUCKET=company-platform-dr
BACKUP_DR_ACCESS_KEY=...
BACKUP_DR_SECRET_KEY=...
# Optional; default is a sibling of the Velero prefix:
# <Velero parent>/cluster-bundles/<project>.
CLUSTER_BACKUP_DR_PREFIX=k8s/cluster-bundles/production
```

Backup, restore, orchestration, and migration commands load `.env`
automatically. Explicitly exported variables still take precedence.
Blank `endpoint` and `bucket` fields in a named profile fall back to
`BACKUP_DR_ENDPOINT` and `BACKUP_DR_BUCKET`; region and prefix also have
environment fallbacks when omitted. Access and secret keys are resolved only
from the process environment and are never appended to the Ansible command
line or written into `platform.yaml`.
Cluster bundles are deliberately stored outside the Velero prefix. Velero
rejects unknown top-level directories inside its own backup store, so an
explicit `CLUSTER_BACKUP_DR_PREFIX` equal to or nested below the configured
Velero prefix fails before publication.

The role rejects `.svc` and SeaweedFS endpoints. Backing up a cluster into the
same cluster is not disaster recovery. Use object lock/versioning and a
separate failure domain where the provider supports them.

`minimal` and `small` leave both scheduled native backups and external DR off
by default. Both can be selected later without rebuilding the foundation when
the active nodes have enough allocatable CPU, memory, and storage; otherwise,
expand or resize through the supported profile-migration workflow first:

```bash
cd platform-orchestrator
./platform.sh enable disaster-recovery
./platform.sh deploy disaster-recovery
```

That selector enables `backup.enabled`, object storage, and
`backup.disaster_recovery.enabled` together. `enable backup` alone intentionally
selects only application-native jobs. A complete `backup-cluster` recovery
point requires the external disaster-recovery layer to have been deployed and
its Velero storage location to be available. Supplying `--dr-endpoint` and
`--dr-bucket` to the disposable tier runner selects both layers for any of the
five profiles.

## Quick Start

Install/update the backup resources through the main playbook, then trigger
the configured CronJobs:

```bash
ansible-playbook -i inventory.yml playbooks/deploy_platform.yml --tags databases,gitlab,backup
./scripts/backup-all.sh --dry-run
./scripts/backup-all.sh --config platform-orchestrator/platform.yaml --force

# Complete encrypted cluster backup. Never pass a passphrase on argv.
export CLUSTER_BACKUP_PASSPHRASE='use-a-secret-manager-value'
./platform-orchestrator/platform.sh backup-cluster \
  --vault-init-file playbooks/.vault-init-k8s.json --force

# Or use an age recipient instead of a shared passphrase.
./platform-orchestrator/platform.sh backup-cluster \
  --vault-init-file playbooks/.vault-init-k8s.json \
  --recipient age1example... --output-dir /secure/offsite/path --force

# Multi-cluster controllers must name the generated secret set explicitly.
./scripts/cluster-backup.sh --config /state/cluster-a/platform.yaml \
  --secrets-file /state/cluster-a/.platform-secrets.yml \
  --vault-init-file /state/cluster-a/.vault-init-cluster-a.json \
  --ssh-known-hosts /state/cluster-a/ssh/known_hosts --force
```

`backup-all.sh` triggers an on-demand full `PerconaPGBackup`, a PBM MongoDB
backup, Vault snapshot, SeaweedFS topology artifact, GitLab archive, and GitLab
Rails secrets for the components enabled by the supplied platform config. An
enabled component that is missing or unhealthy is a backup failure, while a
disabled component is skipped. Each invocation writes a project- and
process-specific `.backup-results-<project>-<timestamp>-<pid>.log`, so parallel
multi-cluster runs cannot merge their audit trails. With `--result-json`, it
also writes a mode-`0600` native recovery catalog containing the exact
PostgreSQL/MongoDB backup CR names, pgBackRest set, backup repository, exact
Job names, restore contract, and artifact prefix for every enabled component.
`cluster-backup.sh` requires that catalog to be complete and stores it as
`application-backups/native-backups.json`. It then
requires a completed Velero resource/PVC backup and captures the control-plane
and cloud recovery bundle. It then uploads the encrypted archive and checksum
to independent DR storage, downloads the archive again, verifies its SHA-256,
and uploads the JSON manifest last as the atomic completion receipt. A remote
archive without that final receipt is interrupted and must not be restored.
Missing layers fail closed; skip options (including `--skip-remote-publish`)
require the explicit `--allow-incomplete` marker, and such a bundle cannot be
restored by `cluster-restore.sh`.

For concurrent or ephemeral clusters, pass a persistent per-cluster
`--ssh-known-hosts` file. Both the bastion and private control-plane connection
use that exact mode-`0600` trust database, preventing private-address collisions
between clusters without weakening host-key checking or rewriting the operator's
global `known_hosts` file.

Before creating the Velero object, the script inventories every non-terminating
PVC. A complete backup requires every one to be `Bound` and actually mounted by
a non-terminating Running pod. It retains a mode-`0600`, machine-readable
`*.pvc-evidence.json` next to the output even if this gate rejects the backup.
The script then requires a completed `PodVolumeBackup` for every recorded
namespace/pod/volume tuple and rejects partial or missing filesystem copies.
Use `--allow-incomplete` only to capture evidence while repairing an obsolete,
unbound, or intentionally unmounted claim; the resulting bundle is not a valid
complete recovery point.

The encrypted archive has an external checksum sidecar and an internal
`SHA256SUMS` manifest. Temporary plaintext is created with mode `0700`/`0600`
and deleted on every exit path. The bundle contains Kubernetes Secrets and PKI;
store the decryption identity separately.

Source recovery includes the `HEAD` Git bundle, one binary patch from `HEAD`
to the final working tree (therefore including both staged and unstaged tracked
changes), and a tar archive of safe non-ignored untracked files. Ignored files
and credential-like paths, symlinks, and special untracked files are never added; generated secrets and
Vault initialization material use their dedicated guarded paths instead. The
manifest records the paths, untracked count, and SHA-256 digest of every source
recovery artifact. It also records the effective bastion, control-plane, and
worker machine types from the generated platform state. After restoring the Git
bundle at the revision in `git-revision.txt` and applying `worktree.patch`, use
the guarded replay command instead of extracting the untracked tar directly:

```bash
./scripts/restore-repository-untracked.sh "$STATE/repository" /path/to/checkout
```

The replay validates the archive against its path inventory and count before
writing, accepts nested regular files, and rejects absolute/traversal names,
links, special members, symlinked parents, tracked destinations, and every
pre-existing destination.

`--secrets-file` prevents a multi-cluster controller from falling back to a
different checkout's generated secret set. The default remains
`playbooks/.platform-secrets.yml` for the normal single-cluster layout.
Whenever `secrets.enabled` is true, `--vault-init-file` is mandatory. The
backup verifies the Ansible Vault header, decryptability through
`ANSIBLE_VAULT_PASSWORD_FILE`, and the required root-token/unseal-share shape
without logging any contents. It copies the still-encrypted file into the
outer encrypted bundle and records that dependency in `MANIFEST.json`. Keep
the Ansible Vault password separately from the bundle and its age identity or
backup passphrase.

Vault restore drills allocate a validated `10Gi` Raft PVC by default. Override
it with `VAULT_RESTORE_STORAGE_SIZE` or `--storage-size 20Gi`; the namespace
quota follows the same value so larger snapshots do not fail behind a hidden
hard-coded quota.

## Profile migration backup gates

Every named-profile transition, including a downgrade to `minimal` or `small`,
temporarily bootstraps the same independent Velero target and requires a
complete encrypted cluster bundle before topology or service changes. A second
bundle is required after target reconciliation. Before each finalization
invocation that still has destructive work pending, `migrate finalize` refreshes
and verifies the final encrypted native+Velero+control-plane recovery point. It
will not retire services, PVCs, or nodes unless that final backup is complete
and fresh. Destructive stages then run from the resumable checkpoint sequence;
Velero is removed only near the end when the target disables scheduled backup.

The migration state stores backup identifiers and generated configs, never the
age identity or passphrase. External backup objects and Loki archive buckets
are not deleted by finalization. Existing retained PVCs are not shrunk in
place; larger safe requests are recorded in `storage-retention.tsv`. SeaweedFS,
Vault Raft, and same-topology VMCluster replica reductions are likewise retained
in `stateful-retention.tsv` instead of risking quorum or shard loss. Obsolete
source-topology PVCs are deleted only after backup and explicit `FINALIZE`
confirmation. VictoriaMetrics topology retirement has an additional independent
data gate: migration writes a deterministic historical sentinel, records exact
source and destination query evidence, and re-queries its value and millisecond
timestamp from the destination immediately before deleting the old CR or PVCs.
Rollback applies the same proof to a post-switch sentinel copied through the
delta window before restoring the old Helm baseline. Backup receipts are not a
substitute for these metrics data proofs, and a completed `vmctl` Job alone is
not accepted as proof.

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

## Full replacement-cluster recovery

Use the same logical project, domain, desired config, generated secrets,
repository state, and Velero prefix. A different provider cluster UID proves
that the restore target is new; changing the logical identity breaks native
backup names, DNS, and object prefixes.

```bash
# 1. Verify and materialize exact state without printing secrets. The output
# directory must not already exist.
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.age \
  --identity /secure/age-identity.txt --mode verify
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.age \
  --identity /secure/age-identity.txt --mode operator-state \
  --output-dir /secure/recovery/k8s

# 2. While the source API is still available, re-download and hash the remote
# receipt, checksum, and encrypted archive before deleting any provider object.
./teardown.sh k8s --confirm k8s \
  --require-backup-receipt /secure/BACKUP.tar.gz.age.manifest.json

# 3. Check out the bundled repository revision and apply its tracked patch.
# Then safely replay untracked regular files before building the replacement
# foundation and external Velero control plane from exact state.
STATE=/secure/recovery/k8s
REVISION=$(<"$STATE/repository/git-revision.txt")
# Clone/fetch repository.bundle, check out $REVISION, then apply worktree.patch.
./scripts/restore-repository-untracked.sh "$STATE/repository" /path/to/checkout
PROJECT=$(yq -r .global.project "$STATE/platform.yaml")
DOMAIN=$(yq -r .global.domain "$STATE/platform.yaml")
EMAIL=$(yq -r .global.email "$STATE/platform.yaml")
ansible-playbook playbooks/deploy_platform.yml -e @"$STATE/platform.yaml" \
  -e "project_name=$PROJECT" -e "domain=$DOMAIN" -e "email=$EMAIL" \
  -e "secrets_file=$STATE/.platform-secrets.yml" \
  -e "vault_init_output_file=$STATE/.vault-init-${PROJECT}.json" \
  --tags infrastructure,network,cluster,velero-bootstrap

# 4. Restore Kubernetes resources and PVC bytes. The command requires a
# Completed source Backup, explicit warning allowances, complete PVR coverage,
# every expected PVC Bound and mounted, Ready replacement nodes, and a fully
# rolled-out Velero controller/node-agent with an Available storage location.
# Warning allowances default to zero.
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.age \
  --identity /secure/age-identity.txt --mode velero \
  --confirm "RESTORE_${PROJECT}"
```

Warning allowances are exact numeric review gates, not a blanket ignore switch.
Inspect the Backup/Restore JSON first, classify every warning, then pass only
the reviewed counts:

```bash
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.age \
  --identity /secure/age-identity.txt --mode velero \
  --allow-backup-warnings 0 --allow-restore-warnings 17 \
  --confirm "RESTORE_${PROJECT}"
```

If the controller stops after the Restore was created, do not create a second
Restore or replay PVC data. Resume validation of that exact object:

```bash
./scripts/cluster-restore.sh --archive /secure/BACKUP.tar.gz.age \
  --identity /secure/age-identity.txt --mode velero \
  --resume-restore "$EXISTING_RESTORE" \
  --allow-backup-warnings 0 --allow-restore-warnings 17 \
  --confirm "RESTORE_${PROJECT}"
```

Resume rejects a deleting object, a different Backup, or any Restore whose PV,
resource exclusions, timeout, namespace/resource scope, node-port, or
existing-resource policy differs from the full replacement contract.
The replacement Restore deliberately excludes cert-manager
`CertificateRequest` objects and Kubernetes `Lease` objects: both contain
cluster-local, short-lived coordination state that must be reissued or
reacquired on the replacement instead of replayed from the source.

`operator-state` creates a new mode-`0700` directory and mode-`0600` files,
including `platform.yaml`, `.platform-secrets.yml`, encrypted Vault init state,
the native catalog, manifest, `git-revision.txt`, and bundled Git recovery inputs. It rejects link,
device, absolute, and traversal archive members and refuses to overwrite an
existing destination. `run_tier.sh` is not the recovery bootstrap command: it
copies a named profile over its configured output. Use the exact bundled config
as shown above.

Velero restores Kubernetes objects and filesystem-backed PVC bytes. It does not
replace application-native replay. It intentionally does not run the full
platform health suite: Shamir-sealed Vault and databases/applications may remain
unready until their exact native artifacts are replayed. The Velero phase stops
only after strict Restore, PodVolumeRestore, PVC mount, replacement-node, and
Velero control-plane gates pass. `native-restore.sh` is the sole owner of the
full profile-aware health gate after native recovery. A new complete backup
writes a schema-v2 `native-backups.json`: its backup ID and SHA-256 are bound into the schema-v2
remote completion receipt. Enabled technologies must have one completed exact
artifact; `latest`, prefix-only Job artifacts, duplicate components, missing
components, and legacy catalogs are rejected by production replay.

After strict Velero restore, plan native replay against the replacement cluster:

`ansible-vault` must be installed, and `ANSIBLE_VAULT_PASSWORD_FILE` must name
a protected regular file able to decrypt the separately protected Vault init
artifact. The replay never disables Vault TLS verification: it uses the
restored internal CA, streams unseal shares over stdin, unseals every restored
member, and waits for an active endpoint before replaying the Raft snapshot.

```bash
STATE=/secure/recovery/k8s
ARCHIVE=/secure/BACKUP.tar.gz.age
RECEIPT=${ARCHIVE}.manifest.json
NATIVE_STATE=/secure/recovery/native-restore-state.json

./scripts/native-restore.sh \
  --catalog "$STATE/native-backups.json" \
  --receipt "$RECEIPT" --archive "$ARCHIVE" \
  --config "$STATE/platform.yaml" --state-file "$NATIVE_STATE" \
  --vault-init-file "$STATE/.vault-init-${PROJECT}.json" \
  --mode plan
```

The plan prints a confirmation bound to the logical project, backup ID, and
replacement `kube-system` namespace UID. Execute it literally:

```bash
./scripts/native-restore.sh \
  --catalog "$STATE/native-backups.json" \
  --receipt "$RECEIPT" --archive "$ARCHIVE" \
  --config "$STATE/platform.yaml" --state-file "$NATIVE_STATE" \
  --vault-init-file "$STATE/.vault-init-${PROJECT}.json" \
  --mode execute \
  --confirm "RESTORE_NATIVE_${PROJECT}_${BACKUP_ID}_${TARGET_CLUSTER_UID}"
```

If the controller or API connection stops, inspect the mode-`0600` state and
rerun the identical command with `--resume`. The script rejects a different
archive hash, catalog hash, receipt hash, config hash, source UID, target UID,
project, or backup ID. Persisted PostgreSQL and GitLab sidecars are also
hash-bound before reuse. Its
application-consistent order is SeaweedFS topology/object and Velero-data
verification, Vault Raft (or the explicitly recorded Velero fallback),
PostgreSQL exact pgBackRest repo2 set, MongoDB exact PBM destination, GitLab
Rails secrets, then the exact GitLab Toolbox archive. Vault, MongoDB, and
GitLab use deterministic Job/CR checkpoints; PostgreSQL preserves its original
CR, operator secrets, and PVC inventory alongside the state before replacing
data. No selected enabled technology is silently skipped. Full platform health
gates must pass before the state becomes `completed`.

PostgreSQL proof is taken from a live `pgbackrest info` query against repo2 and
the exact catalogued set; restored Backup-CR status is not treated as portable
evidence. MongoDB similarly proves the exact catalogued PBM metadata object
because the operator may reconcile and rewrite a restored Backup CR status.

When the accepted backup predates a later credential rotation, preserve the
newer encrypted operator secret files outside the backup. Restore the exact
historical point and all native data first, then run a controlled declarative
reconcile with those retained credentials and repeat authenticated service
checks. Re-enabling a technology or replaying data does not itself rotate
credentials.

Then run component integrity checks, `live-tier-smoke.sh`, bounded load, and a
new complete cluster backup before declaring recovery complete.

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

For PostgreSQL, pass the successful repo2 backup's
`status.backupName` (the pgBackRest set label, not the Kubernetes CR name) and
the source platform project. The dispatcher derives the exact
`<project>-pg` cluster, proves that exactly one successful repo2 backup owns
the label, and restores that set with pgBackRest `--set`:

```bash
PG_SET=$(kubectl get perconapgbackup BACKUP_CR -n databases \
  -o jsonpath='{.status.backupName}')
./scripts/restore-drill.sh --component postgresql \
  --backup "$PG_SET" --project my-platform --namespace pg-restore-drill \
  --force
```

Use `--pg-cluster` instead of `--project` only when the source cluster does not
follow the platform naming convention. `--backup latest` remains available
for exploratory drills, but it is not an exact recovery-point proof.

For a meaningful MongoDB integrity check, identify a stable sentinel:

```bash
./scripts/restore-drill.sh --component mongodb --backup mongodb-backup-20260716 \
  --namespace mongodb-drill-20260716 --force

./scripts/mongodb-restore-drill.sh --backup mongodb-backup-20260716 \
  --verify-database recovery_sentinels \
  --verify-collection backup_markers --min-documents 1
```

An actual Vault drill also requires the original snapshot unseal material,
retained as `config/vault-init.json.vault` in schema-v2 cluster bundles, and a
known path whose restored value must be readable:

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
then cleans up. Its single-replica Deployment uses `Recreate`, so a restart can
reattach the ReadWriteOnce Raft PVC without a rolling-update deadlock.
`VAULT_RESTORE_UNSEAL_KEY` remains a legacy convenience only for a one-share
Vault.

The GitLab artifact drill is an archive-validation drill, not a GitLab service
or database restore. It downloads the selected archive directly into an
isolated PVC, verifies its remote and local sizes, safely extracts it with the
source release's exact Toolbox image, and verifies metadata and repository
payload. The platform configures Toolbox with `--skip db` because GitLab uses
external Percona PostgreSQL; the matching pgBackRest restore drill is a
separate mandatory gate rather than a database dump inside the archive.
Persistent Toolbox staging remains enabled by default. An explicitly
quota-constrained campaign may set `gitlab.backup_persistence_enabled: false`;
that changes only temporary staging to pod-local storage while the completed
archive is still uploaded to S3 before the Job succeeds.
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
