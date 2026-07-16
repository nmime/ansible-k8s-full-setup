# Backup & Restore

Idempotent backup scheduling and verification for enabled platform components.

## Components

| Component   | Backup Method              | CronJob Name              | Namespace   |
|-------------|----------------------------|---------------------------|-------------|
| MongoDB     | PBM (Percona Backup)       | `mongodb-backup`          | `databases` |
| Vault       | Raft snapshot to S3        | `vault-raft-snapshot`     | `vault`     |
| SeaweedFS   | Topology + cluster metadata| `seaweedfs-backup-check`  | `storage`   |
| GitLab data | Official chart Toolbox job | `gitlab-toolbox-backup`   | `gitlab`    |
| GitLab Rails secrets | Secret export to S3 | `gitlab-rails-secrets-backup` | `gitlab` |

## Usage

```bash
ansible-playbook -i inventory -t backup playbooks/deploy_platform.yml
./scripts/backup-all.sh --force
./scripts/restore-drill.sh --component vault --backup vault-20260716T020000Z.snap --dry-run
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
```

## Configuration

All variables are in `defaults/main.yml`. Key settings are `backup_schedule`,
`backup_retention_days`, `backup_storage_bucket`, `backup_alert_enabled`, and
`backup_verify_all`. GitLab application archives are written by the chart to
the `gitlab-backups` bucket. Rails encryption secrets are stored separately;
both artifacts are required for disaster recovery.

## Safety Gates

`backup-all.sh` requires confirmation, validates cluster and object-storage
access, and triggers only deployed components. `restore-drill.sh` and
`gitlab-restore-test.sh` use isolated namespaces and clean them up. The GitLab
script verifies that the archive database can be loaded and repository payload
exists; production recovery must use the official Toolbox restore procedure.
MongoDB does not yet have a verified isolated restore script, and the SeaweedFS
artifact contains topology metadata rather than volume data; both cases fail
closed instead of being reported as successful drills.
