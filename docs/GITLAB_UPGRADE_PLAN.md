# GitLab 18.11 to 19.1 Helm Upgrade Runbook

This runbook covers the supported repository transition from GitLab chart
`9.11.4` (GitLab `18.11.3`) through chart `10.0.4` (GitLab `19.0.4`) to chart
`10.1.2` (GitLab `19.1.2`). It is an operational procedure, not a claim that a
particular cluster has already been upgraded.

GitLab requires Helm deployments to advance one GitLab minor release at a
time. Because `18.11` is already the final required GitLab 18 stop, this path
uses only published GitLab releases. Every step must use the latest
available patch in its minor series and background migrations must finish
before continuing.

Official references:

- <https://docs.gitlab.com/update/upgrade_paths/>
- <https://docs.gitlab.com/charts/installation/upgrade/>
- <https://docs.gitlab.com/charts/installation/version_mappings/>
- <https://docs.gitlab.com/charts/releases/10_0/>
- <https://docs.gitlab.com/charts/backup-restore/>

## Version mapping and path

| Step | Helm chart | GitLab | Purpose |
|---|---:|---:|---|
| Current | 9.11.4 | 18.11.3 | Last GitLab 18 required-stop series |
| Step 1 | 10.0.4 | 19.0.4 | Required major boundary and one-minor transition |
| Step 2 | 10.1.2 | 19.1.2 | Repository target |

Do not jump directly from chart `9.11.4` to `10.1.2`. Do not run this plan
from an older GitLab 18 minor without first following all required `18.2`,
`18.5`, `18.8`, and `18.11` stops that occur after the installed version.

## Chart 10 breaking changes

Chart 10 removes bundled PostgreSQL, Redis, and MinIO, disables the old NGINX
Ingress path by default in favor of Gateway API, and requires PostgreSQL 17 or
newer. This repository already configures external services, but the rendered
values must prove that the transition remains external:

```yaml
postgresql:
  install: false
redis:
  install: false
minio:
  install: false
global:
  psql:
    host: k8s-pg-primary.databases.svc.cluster.local
    database: gitlabhq_production
    username: gitlab
    password:
      secret: gitlab-postgresql-password
      key: password
  redis:
    host: dragonfly.dragonfly.svc.cluster.local
  gitaly:
    internal:
      names: [default]
```

On the chart 9 side of the boundary, `redis.install: false`,
`postgresql.install: false`, and `minio.install: false` document that state is
external. Chart 10 removes those bundled dependencies entirely; the effective
contract is the external service configuration under `global.psql`,
`global.redis`, object storage, and Gitaly `storages`.

The supported chart uses `global.psql.host` and its related database and secret
fields. Gitaly nodes are represented by `global.gitaly.internal` or
`global.gitaly.external`; all configured storages must share the same auth
token.

## Pre-Upgrade Prerequisites

### Maintenance and communication

- [ ] Announce the maintenance window, owner, expected impact, and rollback
      decision time to users and incident responders.
- [ ] Freeze application/configuration changes for the window.
- [ ] Confirm at least 30 minutes of planned downtime for a non-HA deployment.
      HA installations may use rolling updates, but zero downtime is not
      guaranteed.
- [ ] Confirm the on-call operator can access Hetzner, Kubernetes, S3, Vault,
      PostgreSQL, Redis/Dragonfly, and GitLab administrator credentials.

### Cluster and dependency gates

Run the repository checks and stop on any failure:

```bash
python3 scripts/preflight_check.py --project-root "$PWD"
scripts/gitlab-upgrade-check.sh \
  --gitlab-namespace gitlab \
  --s3-endpoint "$OBJECT_STORAGE_ENDPOINT" \
  --s3-bucket "$BACKUP_BUCKET"
kubectl get nodes
kubectl get pods -n gitlab
kubectl get perconapgcluster -n databases
```

- [ ] All nodes are Ready and no Helm release is failed.
- [ ] PostgreSQL is version 17 or newer, is healthy, and has sufficient free
      storage for migrations.
- [ ] Redis/Dragonfly is external, reachable, persistent as required, and not
      replaced by the removed bundled Redis chart.
- [ ] Gitaly healthcheck succeeds from the Toolbox pod. Where available, run
      `gitlab-rake gitlab:gitaly:check` and verify every storage node.
- [ ] Object storage/S3 buckets for artifacts, uploads, packages, registry,
      LFS, and backups are reachable.
- [ ] Pending GitLab background migrations are zero.

### Backup and restore gate

Create a new backup rather than relying only on an old S3 object:

```bash
scripts/backup-all.sh --force
scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
scripts/pg-restore-drill.sh --dry-run --backup-set PGBACKREST_SET
```

The Toolbox archive must include repository/application data. The database is
deliberately excluded from that archive because GitLab uses the external
Percona PostgreSQL cluster; `backup-all.sh` protects it with the native,
major-version-matched pgBackRest workflow. Rails secrets and object-storage
content are separate recovery dependencies. Record the Toolbox backup ID, the
matching PostgreSQL backup set, buckets, sizes, creation times, GitLab and
PostgreSQL versions, and both restore-drill results in the maintenance ticket.
A backup set without recent isolated restore results is not an acceptable
rollback gate.

### Capture exact rollback state

```bash
scripts/upgrade-platform.sh snapshot
helm get values gitlab -n gitlab --all > /secure/path/gitlab-values-before.yaml
helm history gitlab -n gitlab
```

The repository snapshot records exact Helm revisions and `platform.yaml`.
Snapshots are configuration baselines, not data backups.

### Render and inspect both target charts

```bash
helm template gitlab gitlab/gitlab --version 10.0.4 \
  -n gitlab -f /secure/path/gitlab-values-before.yaml >/tmp/gitlab-10.0.4.yaml
helm template gitlab gitlab/gitlab --version 10.1.2 \
  -n gitlab -f /secure/path/gitlab-values-before.yaml >/tmp/gitlab-10.1.2.yaml
```

Check for bundled PostgreSQL, Redis, MinIO, NGINX Ingress, invalid API versions,
unexpected LoadBalancers, missing secrets, changed PVC names, and unpinned
images. Treat any unexplained stateful-resource replacement as CRITICAL.

## Step 1: Upgrade 18.11 to 19.0

1. Enable maintenance mode for a downtime upgrade, or verify the documented
   rolling-update prerequisites for an HA upgrade.
2. Confirm the backup and snapshot identifiers again.
3. Execute only the GitLab component:

   ```bash
   scripts/upgrade-platform.sh execute --component gitlab
   ```

   The script detects chart `9.11.x` and first selects chart `10.0.4`; it does
   not suppress Helm failures and uses `--atomic`, `--reuse-values`, and a
   bounded timeout.
4. Watch pre-migration and post-migration Jobs:

   ```bash
   kubectl get jobs -n gitlab -l release=gitlab -w
   kubectl get pods -n gitlab -w
   ```
5. Wait for all Webservice, Sidekiq, Toolbox, Gitaly, GitLab Shell, Registry,
   KAS, and Runner workloads to be ready.
6. Run `scripts/gitlab-upgrade-check.sh` and stop if any check fails.
7. Confirm background migrations have completed before Step 2.

For an HA rolling procedure, follow GitLab's documented sequence: pause
Webservice and Sidekiq deployments, run the Helm upgrade with post-deployment
migrations initially skipped, wait for pre-migrations, resume Sidekiq, resume
Webservice, then run a second Helm upgrade to execute post-migrations. Ensure
the cluster has capacity for `maxSurge` and uses `maxUnavailable: 0`.

## Step 2: Upgrade 19.0 to 19.1

Repeat the same gates for chart `10.1.2`. The upgrade orchestrator detects
chart `10.0.x` and selects only `10.1.2`.

```bash
scripts/upgrade-platform.sh execute --component gitlab
```

Do not continue if chart `10.0.4` is unhealthy merely because Kubernetes still
has Ready pods from the previous revision. Verify the running image versions,
migration Jobs, and Helm revision directly.

## Step 3: Validate and close the maintenance window

1. Run every post-upgrade check below and record the output.
2. Trigger a new Toolbox backup and the Rails-secret backup, then verify both
   objects exist.
3. Disable maintenance mode only after application, repository, registry,
   object-storage, background-migration, and rollback gates pass.

## Post-Upgrade verification

- [ ] `helm status gitlab -n gitlab` reports deployed chart `10.1.2`.
- [ ] All migrations completed successfully and background migrations are zero.
- [ ] `/-/readiness`, `/-/liveness`, web login, API, SSH clone, HTTPS clone,
      push, merge request, CI job, artifacts, Registry, and package upload work.
- [ ] `gitlab-rake gitlab:check SANITIZE=true` succeeds.
- [ ] Gitaly healthcheck succeeds for every storage node.
- [ ] PostgreSQL and Redis/Dragonfly latency/error rates remain normal.
- [ ] Object storage reads and writes succeed for every configured bucket.
- [ ] Backups run after the upgrade and a new backup is visible in S3.
- [ ] Monitoring, alerting, audit logs, and error budgets show no regression.
- [ ] Maintenance mode is disabled and stakeholders receive completion notice.

## Rollback

### Before incompatible migrations or data writes

If Helm failed before incompatible migrations and GitLab has not accepted new
writes, roll back to the captured exact revision:

```bash
scripts/rollback.sh --component gitlab --snapshot snapshot/upgrade-TIMESTAMP
```

The rollback script reads `helm-revisions.tsv` and calls `helm rollback` with
the recorded revision. It exits nonzero if Helm or health gates fail.

### After a successful 10.x migration

Rolling chart `10.x` back to chart `9.x` is not automatically data-safe. Stop
writes, restore the external PostgreSQL backup, restore the GitLab Toolbox
archive to a working GitLab instance of the same version that created it,
restore Rails secrets and object-storage data, then run the isolated restore
and smoke-test sequence. Never treat `helm rollback` alone as database rollback
across the major boundary.

Use `scripts/pg-restore-drill.sh` for the database and
`scripts/gitlab-restore-test.sh` for the Toolbox archive in isolated namespaces
before directing users to a recovered instance.

## Risk assessment

| Severity | Risk | Mitigation / stop condition |
|---|---|---|
| CRITICAL | Database migration makes chart 9.x rollback unsafe | Fresh S3 backup, Rails secrets, isolated restore, exact snapshot |
| CRITICAL | Bundled stateful dependencies are removed in chart 10 | Render charts and prove external PostgreSQL, Redis, and object storage |
| HIGH | Gitaly nodes or auth token change | Inspect `global.gitaly` and run healthcheck before/after every step |
| HIGH | Background migrations are still running | Stop until they finish; never advance on pod readiness alone |
| HIGH | Object-storage data is missing from recovery | Test every bucket and record independent recovery ownership |
| MEDIUM | Rolling update exceeds cluster capacity | Schedule downtime or reserve capacity for maxSurge |
| MEDIUM | Runner version lags GitLab | Upgrade and test Runner with each target GitLab version |

## Execution record

- [ ] Maintenance ticket and communication owner recorded
- [ ] Current chart/GitLab versions recorded
- [ ] Backup ID, S3 bucket, Rails-secret backup, and restore result recorded
- [ ] Snapshot directory recorded
- [ ] Helm template diffs reviewed
- [ ] Step 1 checks completed
- [ ] Background migrations completed
- [ ] Step 2 checks completed
- [ ] Post-upgrade functional tests completed
- [ ] New post-upgrade backup completed
- [ ] Maintenance window closed and outcome communicated
