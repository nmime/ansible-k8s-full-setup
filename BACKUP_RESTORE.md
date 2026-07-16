# Backup and Restore

## Components covered

| Component | Backup mechanism | CronJob | Namespace |
|---|---|---|---|
| MongoDB | Percona/PBM backup | `mongodb-backup` | `databases` |
| PostgreSQL | Percona Operator pgBackRest | operator-managed | `databases` |
| Vault | Raft snapshot to S3 | `vault-raft-snapshot` | `vault` |
| SeaweedFS | topology and metadata artifact | `seaweedfs-backup-check` | `storage` |
| GitLab | chart Toolbox `backup-utility` | `gitlab-toolbox-backup` | `gitlab` |
| GitLab encryption keys | Rails `secrets.yml` copy to S3 | `gitlab-rails-secrets-backup` | `gitlab` |

Only enabled platform components receive backup resources and verification
requirements. Credentials are generated secrets; insecure static fallbacks are
rejected.

## Quick Start

Install/update the backup resources through the main playbook, then trigger
the configured CronJobs:

```bash
ansible-playbook -i inventory.yml playbooks/deploy_platform.yml --tags backup
./scripts/backup-all.sh --dry-run
./scripts/backup-all.sh --force
```

`backup-all.sh` fails when the cluster, object storage, required CronJob, or
triggered Job fails. It targets each CronJob in its actual component namespace.

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
./scripts/vault-restore-drill.sh --dry-run
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
```

The generic `restore-drill.sh` dispatcher supports only Vault and GitLab. It
fails closed for MongoDB and SeaweedFS because verified isolated restore
implementations do not exist for those components yet. SeaweedFS topology
metadata is not a data backup; protect volume data separately before treating
the object-storage layer as recoverable.

An actual Vault drill also requires the original snapshot unseal material and
a known path whose restored value must be readable:

```bash
export OBJECT_STORAGE_ENDPOINT=https://s3.example.internal
export VAULT_RESTORE_UNSEAL_KEY='...'
export VAULT_RESTORE_TOKEN='...'
export VAULT_RESTORE_VERIFY_PATH='secret/known-recovery-sentinel'
./scripts/vault-restore-drill.sh --snapshot-name vault-20260716T020000Z.snap
```

The GitLab artifact drill downloads and extracts the selected archive, restores
its database dump into isolated PostgreSQL, and verifies repository payload.
For a disaster recovery cutover, restore a same-version GitLab chart with the
official Toolbox `backup-utility --restore`, restore the saved Rails secret,
and follow the GitLab restore runbook. A Helm rollback is not a data restore.

## Safety Gates

1. The selected artifact must exist and be non-empty.
2. Restore namespaces must be isolated from production and carry resource
   limits. Successful scripts delete the namespace unless preservation is
   explicitly requested.
3. Production database clients must never point at the drill namespace.
4. The drill exits nonzero on failed import or payload checks.
5. Rails secrets, object storage, databases, and repository data are all
   separate recovery dependencies.
6. Record artifact ID, source version, size, checks, and cleanup outcome.

## Ongoing verification

The `backup-verification` CronJob checks S3 artifacts daily. Treat this as an
existence/freshness gate, not proof of restorability. Schedule recurring full
restore drills and keep the output with the recovery runbook.
