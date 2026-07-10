# BACKUP_RESTORE.md - Backup & Restore Automation

## Overview

Idempotent backup and restore automation for all critical platform components via
Ansible role (`roles/backup-restore/`) and orchestration scripts.

## Components Covered

| Component   | Backup Method                     | CronJob Name              | Namespace   |
|-------------|-----------------------------------|---------------------------|-------------|
| MongoDB     | Percona Backup for MongoDB (PBM)  | `mongodb-backup`          | `databases` |
| Vault       | Raft snapshot to S3               | `vault-raft-snapshot`     | `vault`     |
| SeaweedFS   | Topology + cluster metadata       | `seaweedfs-backup-check`  | `storage`   |
| GitLab      | Toolbox backup rake task          | `gitlab-backup`           | `gitlab`    |

## Quick Start

```bash
ansible-playbook -i inventory -t backup playbooks/deploy_platform.yml
./scripts/backup-all.sh --force
./scripts/restore-drill.sh --component mongodb --backup daily-20250601-02 --force
kubectl create job --from=cronjob/backup-verification backup-verify-manual-$(date +%Y%m%d) -n backups
```

## Configuration

Variables in `roles/backup-restore/defaults/main.yml` with project overrides in `defaults/main.yml`.

| Variable                    | Default               | Description                        |
|-----------------------------|-----------------------|------------------------------------|
| `backup_schedule`           | `0 2 * * *`           | Cron schedule                      |
| `backup_retention_days`     | `30`                  | Retention period                   |
| `backup_storage_bucket`     | `backups.<domain>`    | S3 bucket                          |
| `backup_alert_enabled`      | `false`               | Enable webhook alerts              |
| `backup_verify_all`         | `true`                | Deploy verification CronJob        |
| `restore_drill_namespace`   | `restore-drill`       | Isolated restore namespace         |

## Safety Gates

### backup-all.sh
1. User confirmation (bypass with `--force`)
2. kubectl connectivity check
3. Object storage reachability
4. Component deployment check (skip if not deployed)
5. Idempotent (skip same-hour backup)

### restore-drill.sh
1. Required `--component` and `--backup` flags
2. Force or dry-run required
3. Backup artifact validation in S3
4. Isolated restore namespace with resource quotas
5. Auto-cleanup after configurable hours
6. No production impact

## Verification

Daily verification CronJob at 06:00 UTC checks MongoDB, Vault, SeaweedFS, and GitLab backup artifacts.

## Alerting

```yaml
backup_alert_enabled: true
backup_alert_webhook_url: "https://hooks.slack.com/services/XXX"
```
Alerts fire at 07:00 UTC after verification.

## Retention

- Artifacts older than `backup_retention_days` (30) are auto-cleaned from S3
- CronJob history: 3 successful, 1 failed retained
- Restore drill namespaces auto-cleaned after 24 hours

## File Structure

```
roles/backup-restore/
├── defaults/main.yml
├── tasks/{main,mongodb_pbm,vault_raft,seaweedfs,gitlab,verification,alerts}.yml
└── README.md
scripts/backup-all.sh, scripts/restore-drill.sh
BACKUP_RESTORE.md
tests/test_backup_restore.py
```
