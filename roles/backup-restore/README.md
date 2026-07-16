# Backup & Restore

Idempotent backup scheduling and verification for enabled platform components.

## Components

| Component   | Backup Method              | Scheduled/on-demand resource | Namespace   |
|-------------|----------------------------|---------------------------|-------------|
| PostgreSQL  | pgBackRest repo1 + S3 repo2 | operator / `PerconaPGBackup` | `databases` |
| MongoDB     | PBM (Percona Backup)       | operator / `PerconaServerMongoDBBackup` | `databases` |
| Vault       | Raft snapshot to S3        | `vault-raft-snapshot`     | `vault`     |
| SeaweedFS   | Topology + cluster metadata| `seaweedfs-backup-check`  | `storage`   |
| GitLab data | Official chart Toolbox job | `gitlab-toolbox-backup`   | `gitlab`    |
| GitLab Rails secrets | Secret export to S3 | `gitlab-rails-secrets-backup` | `gitlab` |
| Kubernetes resources and PVCs | Velero + Kopia node-agent | `full-cluster` | `velero` |

## Usage

```bash
ansible-playbook -i inventory -t backup playbooks/deploy_platform.yml
./scripts/backup-all.sh --force
./scripts/restore-drill.sh --component vault --backup vault-20260716T020000Z.snap --dry-run
./scripts/restore-drill.sh --component mongodb --backup BACKUP_CR --dry-run
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
./scripts/cluster-backup.sh --dry-run
```

## Configuration

All variables are in `defaults/main.yml`. Key settings are `backup_schedule`,
`backup_retention_days`, `backup_storage_bucket`, `backup_alert_enabled`, and
`backup_verify_all`. GitLab application archives are written by the chart to
the `gitlab-backups` bucket. Rails encryption secrets are stored separately;
both artifacts are required for disaster recovery.

For full-cluster DR, set `backup_dr_enabled`, an external
`backup_dr_storage_endpoint`/bucket, and independent
`BACKUP_DR_ACCESS_KEY`/`BACKUP_DR_SECRET_KEY`. The role rejects in-cluster
SeaweedFS as a DR target and deploys pinned Velero with Kopia node agents.

## Safety Gates

`backup-all.sh` requires confirmation, validates cluster and object-storage
access, and triggers only deployed components. `restore-drill.sh` and
`gitlab-restore-test.sh` use isolated namespaces and clean them up. The GitLab
script verifies that the archive database can be loaded and repository payload
exists; production recovery must use the official Toolbox restore procedure.
MongoDB has an isolated operator restore drill with an optional sentinel-data
check. The SeaweedFS topology artifact is metadata only; its data restore is
handled by the external Velero/Kopia replacement-cluster workflow and the
component dispatcher fails closed rather than claiming an isolated restore.
