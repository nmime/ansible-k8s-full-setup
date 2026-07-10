# GitLab 19 Upgrade - Release Notes

> **Status**: IMPLEMENTED (branch `upgrade/gitlab-19-impl`)

## Version Change

| Component      | Before       | After        |
|----------------|-------------|--------------|
| Helm Chart     | `9.11.4`    | `10.1.2`     |
| GitLab App     | `18.11.3`   | `19.1.2`     |

## Breaking Changes Migrated

### 1. `global.psql` -> `global.database.external`
- **Old**: `global.psql.host`, `global.psql.port`, `global.psql.database`
- **New**: `global.database.external.host`, `.port`, `.database`
- Added `global.database.managed: false` to declare external PostgreSQL.
- Our setup uses external PostgreSQL via `k8s-databases` role (pgBouncer -> PostgreSQL).

### 2. Redis Restructure
- **Old**: `redis.install`, `redis.architecture`, `redis.primary.persistence`
- **New**: `redis.enabled`, `redis.replication.enabled`
- Chart 10.x no longer bundles Bitnami Redis subchart.

### 3. Gitaly Storage Array Format
- **Old**: `gitlab.gitaly.persistence.enabled/size/storageClass`
- **New**: `gitlab.gitaly.storages.default.persistentVolumeClaim.size/storageClass` + `.tags`

### 4. Removed `postgresql.install`
- Chart 10.x removed the embedded PostgreSQL subchart entirely.

### 5. Toolbox Backup Cron Format
- **Old**: `backups.cron.enabled/schedule/extraArgs`
- **New**: `backups.schedule` at backup level

## Files Changed

| File | Change |
|------|--------|
| `defaults/main.yml` | `gitlab_chart_version: "10.1.2"` |
| `roles/gitlab-selfhosted/tasks/main.yml` | Chart 10.x migration |
| `docs/GITLAB_UPGRADE_PLAN.md` | Plan -> release notes |
| `tests/unit/test_gitlab_upgrade.py` | New unit tests |
| `tests/component/test_gitlab_config.py` | New component tests |

## Rollback
```bash
git checkout main
git reset --hard origin/main
```

## Pre-flight Checklist
- [ ] Backup GitLab data before merging
- [ ] Verify pgBouncer: `kubectl get svc -n databases`
- [ ] Verify object storage: `kubectl get svc -n storage`
- [ ] Snapshot: `./scripts/snapshot-helm-baseline.sh`

## Post-upgrade Validation
```bash
helm status gitlab -n gitlab
kubectl get deployment gitlab-webservice-default -n gitlab
kubectl get pods -n gitlab
```

## Chart 10.x Reference
- https://gitlab.com/gitlab-org/charts/gitlab/-/blob/v10.1.2/values.yaml
