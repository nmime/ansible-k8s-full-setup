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
changes without contacting Hetzner or Kubernetes.

### Execute and Resume

```bash
export HCLOUD_TOKEN='...'
export BACKUP_DR_ACCESS_KEY='...'
export BACKUP_DR_SECRET_KEY='...'
export CLUSTER_BACKUP_AGE_RECIPIENT=age1...

./platform-orchestrator/platform.sh migrate execute \
  --target production \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" \
  --dr-bucket "$BACKUP_DR_BUCKET" \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"

./platform-orchestrator/platform.sh migrate status
./platform-orchestrator/platform.sh migrate resume
```

The durable stages are:

1. `preflight` — validate config, credentials, external endpoint, tools,
   cluster health, and Hetzner access.
2. `backup` — install/validate external Velero, take native backups, create an
   encrypted etcd/PKI/config/PVC bundle, and capture the Helm baseline.
3. `expand` — add control planes/workers to the maximum source/target topology;
   create spread placement when either side requires it, then verify etcd.
4. `resize` — drain, stop, place, resize, start, wait, and uncordon one node at
   a time; verify etcd after every control-plane change.
5. `apply-target` — reconcile target cluster policy and selected services while
   retaining the expanded topology until sign-off.
6. `migrate-data` — copy VictoriaMetrics history between VMSingle and VMCluster
   in either direction when the topology changes. A deterministic Job is kept
   for resume; a failed partial import is never silently rerun. Loki objects
   remain an external archive when moving to Elasticsearch.
7. `validate` — require Ready nodes, healthy etcd/platform, Bound PVCs, and an
   available external Velero location. Every Deployment/StatefulSet/DaemonSet
   must be fully rolled out, every active Helm release deployed, selected
   PostgreSQL/MongoDB operator CRs Ready, and all cert-manager Certificates
   Ready.
8. `post-backup` — create a second encrypted full-cluster recovery point.

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
Helm baseline, and selects the source profile on the expanded/resized servers.
It never deletes nodes. Once finalization starts, use the recorded recovery
bundle instead of an in-place rollback.

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
