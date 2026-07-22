# Backup & Restore

Idempotent backup scheduling and verification for enabled platform components.

## Components

| Component   | Backup Method              | Scheduled/on-demand resource | Namespace   |
|-------------|----------------------------|---------------------------|-------------|
| PostgreSQL (including GitLab DB) | pgBackRest S3 repo2 (repo1 local fallback) | operator / `PerconaPGBackup` | `databases` |
| MongoDB     | PBM (Percona Backup)       | operator / `PerconaServerMongoDBBackup` | `databases` |
| Vault       | Raft snapshot to S3        | `vault-raft-snapshot`     | `vault`     |
| SeaweedFS   | Topology check + Velero/Kopia PVC data | `seaweedfs-backup-check` / `full-cluster` | `storage` / `velero` |
| GitLab data | Official chart Toolbox job | `gitlab-toolbox-backup`   | `gitlab`    |
| GitLab Rails secrets | Secret export to S3 | `gitlab-rails-secrets-backup` | `gitlab` |

On-demand cluster backups require the schema-v2 native catalog. PostgreSQL
records the exact pgBackRest repo2 set, MongoDB the operator-reported PBM
destination, and the Vault, SeaweedFS, GitLab Rails, and Toolbox Jobs record
the exact final S3 object URI/key. The catalog uses the fixed recovery order
`seaweedfs`, `vault`, `postgresql`, `mongodb`, `gitlab-secrets`, `gitlab`; its
SHA-256 is included in the independently published cluster completion receipt.
| Kubernetes resources and PVCs | Velero + Kopia node-agent | `full-cluster` | `velero` |

## Usage

```bash
ansible-playbook -i inventory -t backup playbooks/deploy_platform.yml
./scripts/backup-all.sh --force
./scripts/restore-drill.sh --component vault --backup vault-20260716T020000Z.snap --dry-run
./scripts/restore-drill.sh --component postgresql --backup PGBACKREST_SET --dry-run
./scripts/restore-drill.sh --component mongodb --backup BACKUP_CR --dry-run
./scripts/restore-drill.sh --component seaweedfs --backup VELERO_BACKUP --dry-run
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
./scripts/cluster-backup.sh \
  --vault-init-file playbooks/.vault-init-k8s.json --dry-run
```

For a replacement cluster, `ansible-playbook ... --tags velero-bootstrap`
installs only the external Velero control plane from this role; it does not
create native backup CronJobs or verification jobs. Pass the exact recovered
platform secrets and encrypted Vault init paths. The ordinary `velero` and
`backup-dr` tags propagate to every dynamically included Velero task.

## Configuration

All variables are in `defaults/main.yml`. Key settings are `backup_schedule`,
`backup_retention_days`, `backup_storage_bucket`, `backup_alert_enabled`, and
`backup_verify_all`. GitLab application archives are written by the chart to
the `gitlab-backups` bucket with `--skip db`: the external Percona PostgreSQL
cluster is backed up separately with its version-matched native tooling in the
same `backup-all.sh` gate. Rails encryption secrets are stored separately. A
GitLab recovery therefore requires the matching PostgreSQL backup, Toolbox
archive, Rails secrets, and object-storage data.
On-demand gates use `repo2` by default so the PostgreSQL restore set is in
S3-compatible object storage; set `BACKUP_POSTGRESQL_REPO=repo1` only for an
explicit local-repository recovery exercise. The gate checks both the
`PerconaPGBackup` and its backing Job so an operator-stuck `Running` state
cannot hide a terminal Job failure. Its bounded wait defaults to 1800 seconds
and can be changed with `BACKUP_POSTGRESQL_TIMEOUT_SECONDS`.

For full-cluster DR, set `backup_dr_enabled`, an external
`backup_dr_storage_endpoint`/bucket, and independent
`BACKUP_DR_ACCESS_KEY`/`BACKUP_DR_SECRET_KEY`. The role rejects in-cluster
SeaweedFS as a DR target and deploys pinned Velero with Kopia node agents.

## Safety Gates

`backup-all.sh` requires confirmation, validates cluster and object-storage
access, and triggers only deployed components. Every `restore-drill.sh` path
uses an isolated namespace and cleans it up by default. The GitLab drill safely
extracts the Toolbox archive and verifies its metadata/repository payload; the
external PostgreSQL database is covered by the separately required pgBackRest
drill. MongoDB has an isolated operator restore drill with an optional sentinel
check. Vault restores all threshold unseal shares and reforms the disposable
Raft copy to one voting peer before verification. SeaweedFS restores one exact
Kopia PodVolumeBackup into a new network-isolated PVC, verifies its snapshot ID
and byte count read-only, and does not join the live quorum. Full SeaweedFS
cutover still requires all related PVCs on a replacement cluster.
