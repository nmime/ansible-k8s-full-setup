# Backup & Restore

Idempotent backup and restore automation for all platform components.

## Components

| Component   | Backup Method              | CronJob Name              | Namespace   |
|-------------|----------------------------|---------------------------|-------------|
| MongoDB     | PBM (Percona Backup)       | `mongodb-backup`          | `databases` |
| Vault       | Raft snapshot to S3        | `vault-raft-snapshot`     | `vault`     |
| SeaweedFS   | Topology + cluster metadata| `seaweedfs-backup-check`  | `storage`   |
| GitLab      | Toolbox backup rake        | `gitlab-backup`           | `gitlab`    |

## Usage

```bash
ansible-playbook -i inventory -t backup playbooks/deploy_platform.yml
./scripts/backup-all.sh --force
./scripts/restore-drill.sh --component mongodb --backup daily-20250601-02 --force
```

## Configuration

All variables in `defaults/main.yml`. Key: `backup_schedule`, `backup_retention_days`, `backup_storage_bucket`, `backup_alert_enabled`, `backup_verify_all`.

## Safety Gates

**backup-all.sh:** confirmation prompt, kubectl check, storage check, deployment check, idempotent.
**restore-drill.sh:** required flags, force/dry-run, S3 validation, isolated namespace with quota, auto-cleanup.
