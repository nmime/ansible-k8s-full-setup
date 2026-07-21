# Upgrade and Profile Migration Runbook

## Overview

There are two deliberately separate workflows:

1. `upgrade-platform.sh` upgrades or reconciles software inside the current
   profile. Its `--tier` option is only a current-tier assertion; a different
   target is rejected.
2. `migrate-profile.sh` changes cluster topology and capability profile. It
   supports all 20 distinct ordered transitions among the five named profiles.

This boundary prevents a Helm upgrade from silently becoming a node expansion,
server resize, service-set change, and data migration.

## Quick Reference

```bash
# Current-profile software upgrade/reconcile
./scripts/upgrade-platform.sh --dry-run plan
./scripts/upgrade-platform.sh preflight
./scripts/upgrade-platform.sh snapshot
./scripts/upgrade-platform.sh --dry-run execute
./scripts/upgrade-platform.sh execute --component argocd
./scripts/upgrade-platform.sh validate

# Profile migration: no cluster mutation during plan
./platform-orchestrator/platform.sh migrate --target production plan
./platform-orchestrator/platform.sh migrate status
./platform-orchestrator/platform.sh migrate execute \
  --target production \
  --dr-endpoint https://s3.example-provider.com \
  --dr-bucket company-platform-dr \
  --backup-recipient age1...
./platform-orchestrator/platform.sh migrate resume
./platform-orchestrator/platform.sh migrate rollback
./platform-orchestrator/platform.sh migrate finalize --backup-recipient age1...

# Exact Helm/config rollback for an ordinary upgrade
./scripts/rollback.sh --snapshot snapshot/upgrade-TIMESTAMP
```

## Prerequisites

Ordinary upgrades require `kubectl`, `helm`, `yq`, `jq`, Python, Ansible, an
accessible cluster, and a valid `platform-orchestrator/platform.yaml`.

Migration additionally requires:

- `hcloud`, SSH access through the recorded bastion, and `HCLOUD_TOKEN`;
- independent external S3-compatible DR storage;
- `BACKUP_DR_ACCESS_KEY` and `BACKUP_DR_SECRET_KEY`;
- either an age recipient (`--backup-recipient`) or
  `CLUSTER_BACKUP_PASSPHRASE` for encrypted recovery bundles;
- enough quota for the maximum source/target control-plane and worker counts to
  coexist before retained nodes are resized.

## Current-Profile Upgrade

### Preflight

```bash
./scripts/upgrade-platform.sh preflight
./scripts/upgrade-platform.sh --dry-run plan
```

Preflight checks required tools, cluster reachability/version, Helm health,
node readiness, local disk space, configuration identity fields, snapshot
availability, and the Git working tree. Do not skip preflight merely to make a
failed maintenance window continue.

### Snapshot and Backup

```bash
./scripts/backup-all.sh --force
./scripts/upgrade-platform.sh snapshot
readlink snapshot/latest
cat snapshot/latest/MANIFEST.yaml
```

The baseline contains the active platform config, exact Helm revisions, all
Helm values/manifests, CRDs, namespaces, cluster RBAC, PV/PVC declarations,
nodes, and Kubernetes version. It is a configuration rollback baseline—not a
data backup. Database, Vault, GitLab, Kubernetes resource/PVC, and full-cluster
recovery layers remain mandatory; see [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

### Canary/Reconcile Phase

The command performs exactly one phase for the current profile. This is called
the canary/reconcile phase in state files for compatibility, but it never walks
through other tiers. For example, a `minimal` cluster can reconcile only
`minimal`; `--tier production` fails and directs the operator to the migration
workflow.

```bash
./scripts/upgrade-platform.sh --tier minimal --dry-run execute
./scripts/upgrade-platform.sh execute --component argocd
./scripts/upgrade-platform.sh execute --component cert-manager
./scripts/upgrade-platform.sh execute --component observability
```

Supported component names are `argocd`, `cilium`, `cert-manager`,
`postgresql`/`database`/`databases`, `observability`, and `gitlab`. A full
execute reconciles the complete platform for the unchanged profile. GitLab is
advanced only through the explicitly supported minor-chart stops and checked
after each stop.

### Health Gates

Every reconcile ends with the health gate suite:

| Gate | Required result |
|---|---|
| Kubernetes nodes | all `Ready` |
| Cilium | expected pods healthy in `kube-system` |
| cert-manager | workloads healthy |
| Argo CD | workloads healthy when selected |
| Databases | PostgreSQL and MongoDB healthy when selected |

An enabled component failure is fatal. A component not selected by the active
profile is reported without manufacturing a failure.

## All-to-All Named Profile Migration

### Plan

```bash
export BACKUP_DR_ENDPOINT=https://s3.example-provider.com
export BACKUP_DR_BUCKET=company-platform-dr
./platform-orchestrator/platform.sh migrate plan \
  --target production \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" --dr-bucket "$BACKUP_DR_BUCKET"
```

The required `--target` accepts `minimal`, `small`, `medium`,
`medium-optimized`, or `production`; it must differ from the active named
profile. The plan generates and validates source, external-backup, expansion,
target-transition, named-target, and rollback configs under the private state
directory. It prints node, component, VictoriaMetrics, and non-shrinking PVC
changes without contacting Hetzner or Kubernetes. Component and alert-channel
selections that differ from the active source profile's named defaults are
carried into the target; the new named profile supplies defaults only for
selections the operator has not customized. The plan records those carried
choices in `selection-retention.tsv`.

The same offline plan writes `volume-capacity-plan.json`. It expands the
generated source and target configs into billable Hetzner volumes (whole GiB,
10 GiB provider minimum), including SeaweedFS, Vault data and audit claims,
database members and pgBackRest repo, GitLab Gitaly, both VictoriaMetrics
topologies, Loki/Elasticsearch, Grafana, Alertmanager, PMM, Dragonfly, Coroot,
Tempo, and Postal. Per-claim growth/target-only capacity and the largest GitLab
backup scratch claim are reported separately. Unknown quantity formats fail the
plan instead of being guessed.

HIPAA-oriented hardening is always carried forward. Profile migration never
schedules its generic removal because host and cluster controls require a
separately reviewed, control-by-control reversal before the active selection can
be changed safely.

### Execute and Resume

Configure these values in the gitignored, mode-`0600` `.env`:

```dotenv
HCLOUD_TOKEN=...
BACKUP_DR_ACCESS_KEY=...
BACKUP_DR_SECRET_KEY=...
CLUSTER_BACKUP_AGE_RECIPIENT=age1...
PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB=1500
PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB=100
```

```bash
./platform-orchestrator/platform.sh migrate execute \
  --target production \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" \
  --dr-bucket "$BACKUP_DR_BUCKET" \
  --volume-quota-gib "$PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB" \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"

./platform-orchestrator/platform.sh migrate status
./platform-orchestrator/platform.sh migrate resume
```

On a multi-cluster controller, bind the migration to the exact generated state
used when that cluster was deployed. The path is persisted in migration state,
so later `resume`, `rollback`, and `finalize` commands reuse it and reject a
conflicting explicit path:

```bash
./platform-orchestrator/platform.sh migrate execute \
  --target production \
  --operator-state-root /state/cluster-a \
  --ssh-key-path /home/operator/.ssh/id_ed25519 \
  --ssh-known-hosts /state/controller-home/.ssh/known_hosts-cluster-a \
  --api-port 16444 \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" \
  --dr-bucket "$BACKUP_DR_BUCKET" \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"
```

`--operator-state-root` derives `.platform-secrets.yml` and
`.vault-init-<project>.json`. Use `--secrets-file` and `--vault-init-file`
instead when those files are stored separately. A mutating migration fails
closed if either exact recovery input is missing or empty. If none of these
options is supplied, the ordinary single-checkout files under `playbooks/`
remain the default.

The controller also resolves and persists the exact private SSH identity and
project-specific known-hosts file. Pass `--ssh-key-path` when an isolated
controller `HOME` does not contain the deployment key, and keep
`--ssh-known-hosts` inside that controller's state rather than falling back to
a shared `~/.ssh/known_hosts`. Resume, rollback, and finalize reject explicit
paths that differ from the recorded values. The private key, its `.pub` file,
and the known-hosts file must be regular readable files; symlinks fail closed.
Pass the cluster's existing controller-local tunnel port with `--api-port` when
it is not the default `16443`. The port is written to every generated migration
config and persisted in state; later commands reject an explicit mismatch so a
resume cannot replace or collide with another controller's API tunnel.

Hetzner's API returns authoritative volume IDs and sizes but does not expose
the account's GiB quota. Therefore live `execute` requires the exact account
quota through `--volume-quota-gib` or
`PROFILE_MIGRATION_HCLOUD_VOLUME_QUOTA_GIB`; do not enter a hoped-for value.
The default 100 GiB reserve is configurable with
`--volume-safety-margin-gib` or
`PROFILE_MIGRATION_VOLUME_SAFETY_MARGIN_GIB`. Preflight requires:

```text
account GiB currently used + estimated remaining migration GiB + safety margin
  <= explicitly configured account quota GiB
```

The first successful check persists the complete provider volume ID/size
baseline, estimator inputs, quota, margin, and result in migration state.
`resume` rejects quota, margin, or generated-plan drift. For a partially
applied target it maps new/grown provider volumes to PV names in this exact
cluster, subtracts only that proven migration consumption, and rechecks the
remaining peak against current account-wide usage. Unmapped or unrelated new
volumes still consume current usage and are never credited to the migration.
The final backup repeats the check before allocating backup scratch.

The durable stages are:

1. `preflight` — validate config, credentials, external endpoint, tools,
   cluster health, Hetzner access, and the persisted fail-closed account volume
   capacity calculation.
2. `backup` — install/validate external Velero, take native backups, create an
   encrypted etcd/PKI/config/PVC bundle, and capture the Helm baseline.
3. `expand` — add control planes/workers to the maximum source/target topology;
   create spread placement when either side requires it, then verify etcd.
4. `resize` — drain, stop, place, resize, start, wait, and uncordon one node at
   a time; verify etcd after every control-plane change and validate the source
   service set that remains authoritative until target reconciliation.
5. `migrate-vault-storage` — complete the guarded offline conversion from any
   legacy file-backed Vault storage to Raft before the target service reconcile.
6. `apply-target` — reconcile target cluster policy and selected services while
   retaining the expanded topology until sign-off.
7. `migrate-data` — copy VictoriaMetrics history between VMSingle and VMCluster
   in either direction when the topology changes. A deterministic Job is kept
   for resume; a failed partial import is never silently rerun. Loki objects
   remain an external archive when moving to Elasticsearch.
8. `validate` — require Ready nodes, healthy etcd/platform, Bound PVCs, and an
   available external Velero location. Every Deployment/StatefulSet/DaemonSet
   must be fully rolled out, every active Helm release deployed, selected
   PostgreSQL/MongoDB operator CRs Ready, and all cert-manager Certificates
   Ready.
9. `post-backup` — create a second encrypted full-cluster recovery point.

Completed stages have durable checkpoint files. `resume` skips only recorded
successes. It does not infer success from partially created resources.
The node-removal order follows the pinned
[Kubespray v2.31 node lifecycle](https://github.com/kubernetes-sigs/kubespray/blob/v2.31.0/docs/operations/nodes.md),
and bidirectional metrics transfer uses the documented
[VictoriaMetrics vmctl native endpoints](https://docs.victoriametrics.com/victoriametrics/vmctl/victoriametrics/).

### Finalize and reclaim the old resource footprint

Execution deliberately retains disabled source services, the old metrics/log
topology, and excess nodes for a sign-off window. After metrics, logs, and
applications are accepted, finalize the transition:

```bash
./platform-orchestrator/platform.sh migrate finalize \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"
```

Finalization has its own resumable checkpoints. It removes disabled dependants
in dependency order, retires VMSingle or VMCluster plus exact obsolete PVCs,
keeps external Loki objects, removes excess workers and then highest-index
control planes with Kubespray `remove-node.yml`, checks etcd around each
control-plane removal, reconciles the named target, captures a final encrypted
native+Velero+control-plane backup, removes Velero last when the target disables
backup, and deletes an unused spread group. After any destructive finalize
checkpoint, rollback is refused and the pre-finalize recovery bundle is the
recovery path.

Existing PVC requests that exceed the target are preserved as explicit target
overrides and listed in `storage-retention.tsv`; Kubernetes cannot shrink a
PVC in place. Data-bearing SeaweedFS, Vault Raft, and an in-place VMCluster are
also never blindly scaled down: required replica overrides are recorded in
`stateful-retention.tsv`. This does not retain superseded component or
metrics-topology PVCs, which are deleted only after the backup and confirmation
gates.

## Rollback

### Ordinary Upgrade Rollback

```bash
./scripts/rollback.sh --snapshot snapshot/upgrade-TIMESTAMP
./scripts/rollback.sh --component argocd --snapshot snapshot/upgrade-TIMESTAMP
./scripts/rollback.sh --dry-run --snapshot snapshot/upgrade-TIMESTAMP
```

Rollback uses recorded exact Helm revisions and the captured platform config.
If new writes or schema changes crossed an incompatible boundary, stop writers
and perform a same-version application-native data restore. Helm rollback is
not a database restore.

### Migration Rollback

```bash
./platform-orchestrator/platform.sh migrate rollback
```

Before destructive finalization, migration rollback copies metrics written
after the target switch back to the source topology, restores the recorded
Helm baseline, removes target-only components in dependency order, and selects
the source profile on the expanded/resized servers. If Vault has already moved
from file storage to Raft, rollback retains that safer storage mode and restores
every non-Vault Helm revision from the baseline. It never deletes nodes.

Target-only data is deleted only after the `post-backup` checkpoint proves that
the encrypted post-migration recovery bundle completed. Before that checkpoint,
rollback removes stateless additions but fails closed on a data-bearing
component instead of discarding writes or reporting a partial rollback as
successful. Resume through `post-backup` or perform an explicitly reviewed
application-native recovery before retrying. Temporary local backup and Velero
resources are removed when the source profile did not select them; remote
recovery objects remain retained. HIPAA-oriented hardening introduced by the
target is also retained because it cannot be reversed generically. Once
finalization starts, use the recorded recovery bundle instead of an in-place
rollback.

## Dry-Run Behavior

`upgrade-platform.sh --dry-run` prints upgrade, backup, snapshot, and health
actions without mutation. `migrate-profile.sh plan` is the authoritative
non-mutating profile diff. `migrate-profile.sh --dry-run execute` prints
pending stages, but the `plan` command should always be reviewed first.

## State Files

- `.upgrade-state/canary-<current-tier>.json` records the single reconcile
  phase retained under the historical filename.
- `.upgrade-state/upgrade-complete.json` records the completed upgrade and
  rollback snapshot.
- `.migration-state/<project>-<source>-to-<target>/state.json` records profile
  identities, topology, status, and the last completed stage.
- `<project>-active-profile-migration` lets lifecycle commands find state after
  the active config becomes transitional.
- `stage-<name>.done` and `finalize-<name>.done` are resumable checkpoints.
- `storage-retention.tsv` explains every larger existing PVC request retained
  in the named target config.
- `stateful-retention.tsv` explains data-bearing replica counts retained until
  a service-specific compaction or member-removal procedure is performed.

State files contain operational metadata and generated configs, not backup
decryption keys. Keep the backup identity or passphrase in a separate secret
manager.

## Troubleshooting

### A tier change is rejected

This is intentional. Run `platform.sh migrate --target PROFILE plan`; do not
edit the current tier and rerun the upgrade script.

### Migration stops after node expansion or resize

Run `platform.sh migrate status`, inspect Kubernetes node state, the Hetzner
server type/placement, and etcd health, then use `migrate resume`. Do not mark a
checkpoint complete manually.

### Backup stage fails

Require the external `BackupStorageLocation` to be `Available`, inspect the
native backup CRs/jobs, and verify the independent S3 credentials and age
recipient/passphrase. Never continue into node mutation with an incomplete
bundle.

### Health gate fails

Inspect the named namespace, Helm release, operator CR status, PVC state, and
recent events. Use the recorded exact snapshot for rollback only after
identifying whether the failure is configuration or data related.

### Rollback cannot repair data

Restore the appropriate native artifact or replacement-cluster Velero copy
from [BACKUP_RESTORE.md](BACKUP_RESTORE.md). Do not repeatedly roll Helm while
writes continue.
