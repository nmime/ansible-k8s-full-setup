# Upgrade and Profile Migration Runbook

## Overview

There are two deliberately separate workflows:

1. `upgrade-platform.sh` upgrades or reconciles software inside the current
   profile. Its `--tier` option is only a current-tier assertion; a different
   target is rejected.
2. `migrate-profile.sh` changes cluster topology and capability profile. The
   verified path is currently `minimal` to `production`.

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
./platform-orchestrator/platform.sh migrate plan
./platform-orchestrator/platform.sh migrate status
./platform-orchestrator/platform.sh migrate execute \
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
- enough quota for three control-plane and three worker servers to coexist
  before old nodes are resized.

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

## Minimal to Production Migration

### Plan

```bash
export BACKUP_DR_ENDPOINT=https://s3.example-provider.com
export BACKUP_DR_BUCKET=company-platform-dr
./platform-orchestrator/platform.sh migrate plan \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" --dr-bucket "$BACKUP_DR_BUCKET"
```

The plan generates and validates four configs under the private migration
state directory: external-backup bootstrap, expanded minimal capability,
production target, and non-destructive rollback. It prints the source/target
diff and performs no Hetzner or Kubernetes mutation.

### Execute and Resume

```bash
export HCLOUD_TOKEN='...'
export BACKUP_DR_ACCESS_KEY='...'
export BACKUP_DR_SECRET_KEY='...'
export CLUSTER_BACKUP_AGE_RECIPIENT=age1...

./platform-orchestrator/platform.sh migrate execute \
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
3. `expand` — create spread placement and add control planes/workers until the
   production six-node topology is healthy; verify etcd.
4. `resize` — drain, stop, place, resize, start, wait, and uncordon one node at
   a time; verify etcd after every control-plane change.
5. `apply-production` — activate the production config and reconcile all
   production services.
6. `migrate-data` — copy VictoriaMetrics history from VMSingle to VMCluster.
   The former Loki object-store history is retained as an archive while new
   production logs use Elasticsearch.
7. `validate` — require Ready nodes, healthy etcd/platform, Bound PVCs, and an
   available external Velero location. Every Deployment/StatefulSet/DaemonSet
   must be fully rolled out, every active Helm release deployed, selected
   PostgreSQL/MongoDB operator CRs Ready, and all cert-manager Certificates
   Ready.
8. `post-backup` — create a second encrypted full-cluster recovery point.

Completed stages have durable checkpoint files. `resume` skips only recorded
successes. It does not infer success from partially created resources.

### Finalize and reclaim the old resource footprint

The completed migration deliberately retains the old VMSingle and Loki
workloads for a sign-off window. After metrics, logs, and applications are
accepted, finalize the transition:

```bash
./platform-orchestrator/platform.sh migrate finalize \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"
```

Finalization records the exact old PVCs, removes VMSingle/Loki/Promtail and
those PVCs, keeps Loki's SeaweedFS object archive, re-runs health gates, and
takes a final encrypted native+Velero+control-plane backup. This is the step
that releases the superseded minimal-profile compute and block storage. After
finalization, an application rollback requires restoring the pre-finalize
recovery bundle; the command refuses to create an empty minimal data plane.

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

Migration rollback restores the recorded Helm/config baseline and selects the
minimal capability set on the expanded, production-sized servers. It never
automatically deletes control planes or workers; capacity reduction is a
separate reviewed operation after recovery and backup verification.

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
- `.migration-state/<project>-minimal-to-production/state.json` records
  migration status and the last completed stage.
- `stage-<name>.done` files are the resumable migration checkpoints.

State files contain operational metadata and generated configs, not backup
decryption keys. Keep the backup identity or passphrase in a separate secret
manager.

## Troubleshooting

### A tier change is rejected

This is intentional. Run `platform.sh migrate plan`; do not edit the current
tier and rerun the upgrade script.

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
