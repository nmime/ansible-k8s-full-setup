# GitLab Major Upgrade Plan: 18.11 → 19.1

## Summary

| Item              | Current                  | Target                    |
|-------------------|--------------------------|---------------------------|
| GitLab app        | `18.11.3`                | `19.1.2`                  |
| Helm chart        | `9.11.4`                 | `10.1.2`                  |
| PostgreSQL source | `global.psql` (chart 9.x) | `global.applicationSettings.database` (chart 10.x) |
| Redis             | `redis.install: true` (sub-chart) | External Redis refactoring in chart 10.x |
| Gitaly            | `gitaly.persistence`     | `gitaly.nodes` (new schema) |
| Expected downtime | N/A (planned)            | **5–15 min** (per step, rolling) |

> **WARNING:** This is a **major version upgrade** spanning GitLab 18.x → 19.x AND Helm chart 9.x → 10.x. GitLab requires upgrading through every minor version (18.11 → 18.17 → 19.0 → 19.1). Chart 10.x introduces **breaking changes** that require manual values migration. Skipping versions is **NOT** supported.

---

## 1. Incremental Upgrade Path

GitLab's database migrations **must** run sequentially through every minor version. Each step is a Helm upgrade that applies migrations before the next step.

```
18.11.3 ──► 18.17.x ──► 19.0.x ──► 19.1.x
   │           │           │           │
   │           │           │           └─ Target: 19.1.2 (chart 10.1.2)
   │           │           └─ Major release (chart 10.x breakage)
   │           └─ Last 18.x minor
   └─ Current: 18.11.3 (chart 9.11.4)
```

### Per-step Helm chart and app version mapping

| Step | GitLab app version | Helm chart version | Notes |
|------|--------------------|--------------------|-------|
| 0 (current) | 18.11.3 | 9.11.4 | Production baseline |
| 1 | 18.17.x (latest 18.17 patch) | 9.11.4 or latest 9.x | Stay on chart 9.x |
| 2 | 19.0.x (latest 19.0 patch) | 10.0.x (latest 10.0 patch) | **Chart migration** |
| 3 | 19.1.2 | 10.1.2 | Final target |

> **CRITICAL:** Steps 1 and 2 require **chart 9.x** values compatibility. Step 2 introduces chart 10.x breaking changes. See Section 3 for the values migration.

### Chart 10.x Breaking Changes

| Removed/Changed Key | Chart 9.x Value | Chart 10.x Replacement |
|---------------------|-----------------|------------------------|
| `global.psql.host` | `k8s-pg-pgbouncer.databases.svc.cluster.local` | `global.applicationSettings.database.host` |
| `global.psql.port` | `5432` | `global.applicationSettings.database.port` |
| `global.psql.database` | `gitlabhq_production` | `global.applicationSettings.database.name` |
| `global.psql.username` | `gitlab` | `global.applicationSettings.database.username` |
| `global.psql.password` | `{secret: ..., key: ...}` | `global.applicationSettings.database.password` (new structure) |
| `redis.install: true` | Sub-chart managed Redis | External Redis with `redis.external` block |
| `redis.primary.persistence` | Sub-chart PVC | Removed; use external Redis persistence |
| `gitaly.persistence.size` | Single PVC | `gitaly.nodes[0].persistence.size` (array-based) |
| `gitaly.resources` | Pod-level | `gitaly.nodes[0].resources` (per-node) |
| `global.redis` | Sub-chart config | `redis.external.host`, `redis.external.password` |

> **Source:** [GitLab Helm chart CHANGELOG](https://gitlab.com/gitlab-org/charts/gitlab/-/blob/master/CHANGELOG.md) — version 10.0.0 release notes.

---

## 2. Pre-Upgrade Prerequisites

### 2.1 GitLab Backup

A toolbox backup must be taken **immediately before** the first step.

- The backup tarball must be uploaded to S3-compatible storage (SeaweedFS-backed).
- Verify the backup file exists in `s3://{{ backup_storage_bucket }}/{{ backup_project }}/gitlab/` with a timestamp within 24 hours.

**Command:**
```bash
# Trigger backup via the existing CronJob or manually:
kubectl create job --from=cronjob/gitlab-backup gitlab-backup-pre-upgrade-$(date +%Y%m%d) -n gitlab
# Wait for completion:
kubectl wait --for=condition=complete job/gitlab-backup-pre-upgrade-$(date +%Y%m%d) -n gitlab --timeout=3600s
# Verify in S3:
aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 ls "s3://backups.${BACKUP_PROJECT}/gitlab/" | grep gitlab- | tail -1
```

### 2.2 PostgreSQL Connection Verification

GitLab uses an external PostgreSQL database. Verify connectivity before upgrade:

```bash
# Check PostgreSQL pods are healthy
kubectl get pods -n databases | grep pg
# Test GitLab can connect to the database:
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rails db:check 2>&1 | head -5
```

### 2.3 Gitaly Storage Verification

Gitaly stores all repository data. Verify its health and storage:

```bash
# Check Gitaly pods
kubectl get pods -n gitlab -l app=gitaly
# Verify Gitaly storage:
kubectl exec -n gitlab deploy/gitlab-gitaly -- gitaly-prrc healthcheck 2>&1 | tail -10
# Check storage PVC capacity:
kubectl get pvc -n gitlab | grep gitaly
```

### 2.4 Redis Verification

Before migrating from sub-chart Redis to external Redis:

```bash
# Check Redis pod (chart 9.x):
kubectl get pods -n gitlab -l app.kubernetes.io/name=redis
# Verify Redis is responding:
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rails runner "puts Redis.current.ping"
```

### 2.5 Object Storage Buckets Verified

GitLab uses S3-compatible storage for uploads, LFS, artifacts, packages, etc. Verify all buckets exist:

```bash
for bucket in gitlab-uploads gitlab-lfs gitlab-artifacts gitlab-packages gitlab-terraform-state gitlab-ci-secure-files gitlab-dependency-proxy gitlab-registry gitlab-backups gitlab-tmp; do
  aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 ls "s3://$bucket/" >/dev/null 2>&1 && echo "OK: $bucket" || echo "MISSING: $bucket"
done
```

### 2.6 Preflight Check Script

Run the automated preflight check before proceeding:

```bash
./scripts/gitlab-upgrade-check.sh
```

All checks must report **PASS** before starting the upgrade.

### 2.7 Review Current Helm Values

Capture the current deployed values for reference during migration:

```bash
helm get values gitlab -n gitlab > snapshot/gitlab-values-pre-upgrade.yaml
helm get manifest gitlab -n gitlab > snapshot/gitlab-manifest-pre-upgrade.yaml
```

---

## 3. Chart 9.x → 10.x Values Migration

Before step 2 (19.0.x), you must migrate Helm values. The following diff shows the structural changes:

### 3.1 PostgreSQL (`global.psql` removal)

**Chart 9.x:**
```yaml
global:
  psql:
    host: 'k8s-pg-pgbouncer.databases.svc.cluster.local'
    port: 5432
    database: gitlabhq_production
    username: gitlab
    password:
      secret: gitlab-postgresql-password
      key: postgresql-password
    preparedStatements: false
```

**Chart 10.x:**
```yaml
global:
  applicationSettings:
    database:
      host: 'k8s-pg-pgbouncer.databases.svc.cluster.local'
      port: 5432
      name: gitlabhq_production
      username: gitlab
      password:
        secret: gitlab-postgresql-password
        key: postgresql-password
      preparedStatements: false
```

### 3.2 Redis (sub-chart → external)

**Chart 9.x:**
```yaml
redis:
  install: true
  architecture: standalone
  primary:
    persistence:
      enabled: true
      size: 8Gi
```

**Chart 10.x:**
```yaml
redis:
  install: false
  external:
    host: 'gitlab-redis.gitlab.svc.cluster.local'
    port: 6379
    # If migrating to a dedicated Redis, provide password:
    # password: ...
```

> **NOTE:** For the initial 10.x migration, you may keep the existing sub-chart Redis pod running as an external service. The `redis.install: true` sub-chart is no longer bundled; you may deploy Redis separately or use Dragonfly (already in the platform).

### 3.3 Gitaly (single instance → nodes array)

**Chart 9.x:**
```yaml
gitlab:
  gitaly:
    persistence:
      enabled: true
      size: '50Gi'
    resources:
      requests:
        cpu: 200m
        memory: 512Mi
```

**Chart 10.x:**
```yaml
gitlab:
  gitaly:
    nodes:
      - name: default
        storage:
          storagePath: /var/opt/gitlab/git-data
        persistence:
          enabled: true
          size: '50Gi'
        resources:
          requests:
            cpu: 200m
            memory: 512Mi
```

### 3.4 Applying the Migration

1. Copy `snapshot/gitlab-values-pre-upgrade.yaml` to a working copy.
2. Apply the structural transformations above.
3. Validate with `helm template` before applying:
   ```bash
   helm template gitlab gitlab/gitlab --version 10.0.0 -f migrated-values.yaml --validate -n gitlab > /dev/null
   ```
4. Apply with `helm upgrade`:
   ```bash
   helm upgrade gitlab gitlab/gitlab --version 10.0.0 -f migrated-values.yaml -n gitlab --wait --timeout 60m
   ```

---

## 4. Per-Step Upgrade Procedure

### Step 1: 18.11.3 → 18.17.x (chart stays on 9.x)

1. **Take a backup** (Section 2.1).
2. Update `gitlab_chart_version` in `roles/gitlab-selfhosted/tasks/main.yml` to latest 9.x chart.
3. Set `global.gitlabVersion: "18.17.x"` (latest 18.17 patch) in Helm values.
4. Run:
   ```bash
   helm upgrade gitlab gitlab/gitlab --version 9.12.0 \
     -f snapshot/gitlab-values-pre-upgrade.yaml \
     --set global.gitlabVersion="18.17.8" \
     -n gitlab --wait --timeout 60m
   ```
5. **Verify** (Section 4.2).

### Step 2: 18.17.x → 19.0.x (chart 9.x → 10.x migration)

1. **Take a backup** (Section 2.1) — fresh backup before chart migration.
2. **Migrate values** (Section 3) — this is the critical step.
3. Run:
   ```bash
   helm upgrade gitlab gitlab/gitlab --version 10.0.0 \
     -f snapshot/gitlab-values-migrated.yaml \
     --set global.gitlabVersion="19.0.6" \
     -n gitlab --wait --timeout 60m
   ```
4. **Verify** (Section 4.2) — extra attention on PostgreSQL and Redis connectivity.

### Step 3: 19.0.x → 19.1.2 (chart 10.x)

1. **Take a backup** (Section 2.1).
2. Update to target versions:
   ```bash
   helm upgrade gitlab gitlab/gitlab --version 10.1.2 \
     -f snapshot/gitlab-values-migrated.yaml \
     --set global.gitlabVersion="19.1.2" \
     -n gitlab --wait --timeout 60m
   ```
3. **Verify** (Section 4.2).

### 4.1 Post-Step Verification (each step)

```bash
# Check all GitLab pods are running
kubectl get pods -n gitlab

# Check GitLab Rails application status
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rails runner "puts Gitlab::CurrentSettings.version_string"

# Verify database migration completed
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rake gitlab:db:prepare STATUS=done

# Check Gitaly health
kubectl exec -n gitlab deploy/gitlab-gitaly -- gitaly-prrc healthcheck 2>&1 | tail -5

# Check Redis connectivity
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rails runner "puts Redis.current.ping"

# Verify GitLab API responds
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- curl -s http://localhost:8080/api/v4/version

# Check backup CronJob is still configured
kubectl get cronjob -n gitlab | grep backup
```

### 4.2 Smoke Tests

```bash
# Test GitLab web UI is accessible
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- curl -s -o /dev/null -w "%{http_code}" http://localhost:8080/

# Test runner connectivity
kubectl get pods -n gitlab -l app=runner

# Verify CI pipelines can be triggered (if applicable)
kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rails runner "puts Gitlab::CurrentSettings.ci_enabled"
```

---

## 5. Post-Upgrade Tasks

### 5.1 Update Version References in Ansible

After a successful upgrade to 19.1.2:

- Update `defaults/main.yml`:
  ```yaml
  # renovate: datasource=helm depName=gitlab
  gitlab_chart_version: "10.1.2"
  ```
- Update `roles/gitlab-selfhosted/tasks/main.yml`:
  ```yaml
  gitlab_chart_version: 10.1.2
  # In Helm values:
  global:
    gitlabVersion: 19.1.2
  ```
- Update values to reflect chart 10.x structure (migrated values).

### 5.2 Update Renovate Configuration

Ensure Renovate can track the new chart version in `.renovaterc.json`:

```json
{
  "packageRules": [
    {
      "matchDatasources": ["helm"],
      "matchPackageNames": ["gitlab"],
      "allowedVersions": ">=10.0.0"
    }
  ]
}
```

### 5.3 Verify Backup CronJob

After chart 10.x migration, the GitLab toolbox backup CronJob must still reference the correct toolbox image and namespace:

```bash
kubectl get cronjob gitlab-backup -n gitlab -o yaml | grep -A5 image
# Verify the CronJob uses a 19.x compatible image
```

### 5.4 Post-Upgrade Checklist

- [ ] All GitLab pods in `Running` state
- [ ] Database migration confirmed: `gitlab:db:prepare STATUS=done`
- [ ] Gitaly storage health check passes
- [ ] Redis `PING` returns `PONG`
- [ ] GitLab API `/api/v4/version` returns `19.1.2`
- [ ] Object storage buckets accessible
- [ ] Backup CronJob runs successfully
- [ ] GitLab runners registered and healthy
- [ ] Git web access (SSH/HTTP) functional
- [ ] CI pipeline test run passed

---

## 6. Rollback Procedures

### 6.1 Rollback Within Same Major (19.x)

If a step within chart 10.x fails:

```bash
# Rollback to previous Helm revision
helm rollback gitlab -n gitlab --timeout 60m
# Verify
kubectl rollout status deploy/gitlab-gitlab-rails -n gitlab --timeout=10m
```

### 6.2 Rollback from Chart 10.x to 9.x (step 2 failure)

If the chart 10.x migration fails:

1. **Revert Helm values** to chart 9.x structure:
   ```bash
   helm upgrade gitlab gitlab/gitlab --version 9.12.0 \
     -f snapshot/gitlab-values-pre-upgrade.yaml \
     --set global.gitlabVersion="18.17.x" \
     -n gitlab --wait --timeout 60m
   ```
2. Verify database integrity (migrations may be partial).
3. If database migration was partially applied, run:
   ```bash
   kubectl exec -n gitlab deploy/gitlab-gitlab-rails -- bundle exec rake gitlab:db:prepare
   ```

### 6.3 Full Restore from Backup (catastrophic failure)

If all else fails, restore from the pre-upgrade backup:

1. Create an isolated restore namespace:
   ```bash
   ./scripts/gitlab-restore-test.sh --restore --backup <backup-timestamp>
   ```
2. Follow the restore drill procedure to validate the backup.
3. Once validated, perform a production restore (requires manual intervention).

### 6.4 Restore Test Script

```bash
# Test restore in isolated namespace (no production impact)
./scripts/gitlab-restore-test.sh --dry-run
./scripts/gitlab-restore-test.sh --restore --backup <latest-backup-timestamp> --namespace restore-drill
```

---

## 7. Risk Assessment

| Risk | Severity | Likelihood | Mitigation |
|------|----------|------------|------------|
| Database migration hangs | **HIGH** | Medium | Time-box each migration; have rollback procedure ready |
| Chart 10.x values mismatch | **HIGH** | High | Validate with `helm template --validate` before upgrade |
| Gitaly data corruption | **CRITICAL** | Low | Pre-upgrade backup; verify with `gitaly-prrc healthcheck` |
| Redis data loss on sub-chart removal | **HIGH** | Medium | Keep old Redis pod running as external service during transition |
| `global.psql` removal breaks connectivity | **HIGH** | High | Migrate values carefully; test with `rails db:check` |
| Backup CronJob incompatible with new chart | **MEDIUM** | Medium | Update CronJob image tag to match new GitLab version |
| Object storage connection fails | **MEDIUM** | Low | Verify all buckets before upgrade (Section 2.5) |
| Long downtime per step | **MEDIUM** | Low | Each step expected 5-15 min; schedule during maintenance window |

### Risk Summary

- **CRITICAL:** 1 (Gitaly data corruption — mitigated by backup + healthcheck)
- **HIGH:** 4 (database migration, values migration, Redis, PostgreSQL)
- **MEDIUM:** 3 (CronJob compatibility, object storage, downtime)

### Recommended Maintenance Window

- **Minimum:** 2 hours for all 3 steps (assuming smooth execution)
- **Recommended:** 4 hours (includes rollback buffer and post-upgrade validation)
- **Schedule:** During lowest CI/CD activity period

---

## 8. Communication Plan

| Phase | Audience | Channel | Message |
|-------|----------|---------|---------|
| Pre-upgrade (24h before) | Team | Slack `#devops` | Upgrade window announced; backup status |
| Start of maintenance | Team + stakeholders | Slack + email | Maintenance started; expected duration |
| Each step complete | Team | Slack `#devops` | Step N complete; X of 3 steps done |
| Upgrade complete | Team + stakeholders | Slack + email | Upgrade complete; validation results |
| Rollback (if needed) | Team + stakeholders | Slack + email | Rollback initiated; ETA for restoration |

---

## 9. Execution Checklist

### Pre-Upgrade

- [ ] Read and understand this entire plan
- [ ] Run `./scripts/gitlab-upgrade-check.sh` — all checks PASS
- [ ] Pre-upgrade backup taken and verified in S3
- [ ] Current Helm values captured: `snapshot/gitlab-values-pre-upgrade.yaml`
- [ ] Chart 10.x migrated values prepared and validated with `helm template`
- [ ] Maintenance window scheduled and communicated
- [ ] Rollback plan reviewed (Section 6)
- [ ] Restore drill tested: `./scripts/gitlab-restore-test.sh --dry-run`

### Step 1: 18.11 → 18.17

- [ ] Fresh backup taken
- [ ] Helm upgrade to 18.17.x with chart 9.x
- [ ] Post-step verification (Section 4.1)
- [ ] Smoke tests pass (Section 4.2)

### Step 2: 18.17 → 19.0 (chart migration)

- [ ] Fresh backup taken
- [ ] Values migrated from 9.x to 10.x schema
- [ ] `helm template --validate` passes
- [ ] Helm upgrade to 19.0.x with chart 10.x
- [ ] PostgreSQL connectivity verified (`rails db:check`)
- [ ] Redis connectivity verified (`Redis.current.ping`)
- [ ] Gitaly health check passes
- [ ] Post-step verification (Section 4.1)
- [ ] Smoke tests pass (Section 4.2)

### Step 3: 19.0 → 19.1.2

- [ ] Fresh backup taken
- [ ] Helm upgrade to 19.1.2 with chart 10.1.2
- [ ] Post-step verification (Section 4.1)
- [ ] Smoke tests pass (Section 4.2)

### Post-Upgrade

- [ ] Version references updated in Ansible (`defaults/main.yml`, `roles/gitlab-selfhosted/`)
- [ ] Backup CronJob verified working with 19.x image
- [ ] Post-upgrade checklist complete (Section 5.4)
- [ ] Communication sent: upgrade complete
- [ ] Monitoring reviewed for anomalies
