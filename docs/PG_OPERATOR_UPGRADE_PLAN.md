# Percona PostgreSQL Operator Upgrade and Recovery Runbook

## Current contract

This repository deploys:

| Item | Value |
|---|---|
| Operator Helm chart | `percona/pg-operator 3.0.0` |
| Cluster API | `pgv2.percona.com/v2` |
| Cluster kind | `PerconaPGCluster` |
| Default cluster | `k8s-pg` (or `<project>-pg`) |
| PostgreSQL major | `18` |
| Local backup repository | `repo1` on a PVC |
| Off-cluster repository | `repo2` in the `backups` S3 bucket |
| S3 repository path | `/pgbackrest/<project>-pg/repo2` |

The operator 3.0 schema still uses `configuration` for pgBackRest secrets and
`repo2-path` in the pgBackRest global settings. `repoConfiguration`,
`s3.keyPrefix`, `postgresql.percona.com/v2`, and `PostgresCluster` are not the
resource contract deployed by this repository.

## Safety rules

- Never change PostgreSQL major version and operator major version in the same
  maintenance window.
- Never delete the source cluster, its PVCs, or backup objects until an
  isolated restore has succeeded.
- A Helm rollback does not roll database data back.
- Keep writes quiesced between the final backup and any production cutover.
- Record the source cluster SHA, chart version, backup label, S3 path, and
  verification output.

## Preflight

Run the repository check first:

```bash
OBJECT_STORAGE_ENDPOINT=https://s3.example.internal \
PGBACKREST_BUCKET=backups \
./scripts/pg-upgrade-check.sh --pg-cluster k8s-pg
```

The check validates tooling, the Helm release, `PerconaPGCluster`, the primary
pod, replica lag, pgBackRest metadata, S3 repository contents, PVC capacity,
PgBouncer, and chart availability. Any failed check blocks the change.

Confirm the live resource and backup configuration directly:

```bash
kubectl get perconapgcluster k8s-pg -n databases -o yaml
kubectl get pod -n databases \
  -l postgres-operator.crunchydata.com/cluster=k8s-pg
kubectl get perconapgbackup -n databases
```

Create an explicit on-demand full backup when required:

```yaml
apiVersion: pgv2.percona.com/v2
kind: PerconaPGBackup
metadata:
  generateName: k8s-pg-pre-upgrade-
  namespace: databases
spec:
  pgCluster: k8s-pg
  repoName: repo2
  options:
    - --type=full
```

Apply the resource, wait for its status to become successful, and record
`status.backupName`. Verify objects exist below
`s3://backups/pgbackrest/k8s-pg/repo2/`.

## Restore drill

Start with the non-mutating plan:

```bash
./scripts/pg-restore-drill.sh --dry-run --pg-cluster k8s-pg
```

For execution, set the S3 endpoint. The script copies the source
`pgbackrest-s3-creds` Secret into an isolated namespace, installs the pinned
operator, creates a new `pgv2.percona.com/v2` `PerconaPGCluster` with a
`dataSource.pgbackrest` source, then verifies databases, tables, extensions,
version, replication, and client connectivity.

```bash
export OBJECT_STORAGE_ENDPOINT=https://s3.example.internal
./scripts/pg-restore-drill.sh --pg-cluster k8s-pg
```

The isolated S3 clone path restores the latest valid backup. A specific
pgBackRest label must be tested with a disposable cluster and a
`PerconaPGRestore` resource; the script deliberately rejects a non-`latest`
`--backup-set` rather than silently restoring the wrong backup.

## Operator chart upgrade

1. Read the release notes and supported upgrade path for every intermediate
   chart version.
2. Pass the preflight and restore drill above.
3. Export the live Helm values and `PerconaPGCluster` resource.
4. Quiesce application writes and create a final full `repo2` backup.
5. Upgrade one chart step with `--atomic --wait` and an explicit version.
6. Wait for the operator and cluster status to become ready.
7. Verify the primary, replicas, PgBouncer, application reads/writes,
   scheduled backups, and monitoring.
8. Re-enable writes only after every gate passes.

Do not use `--reuse-values` across an operator major boundary without first
diffing the complete values schema.

## In-place data restore

An in-place restore is destructive. Use a `PerconaPGRestore` only after the
isolated drill has passed and writes are stopped:

```yaml
apiVersion: pgv2.percona.com/v2
kind: PerconaPGRestore
metadata:
  name: k8s-pg-restore
  namespace: databases
spec:
  pgCluster: k8s-pg
  repoName: repo2
  options:
    - --type=immediate
    - --set=BACKUP_LABEL
```

Track the restore resource, operator logs, and cluster state until the cluster
is ready. Validate row counts and application invariants before reopening
traffic.

## Rollback

- Before data mutation, an atomic Helm failure may be rolled back to the exact
  recorded chart revision.
- After a data restore starts, do not treat Helm rollback as recovery. Restore
  the selected backup into a clean cluster or follow the operator's failed
  restore recovery procedure.
- Preserve the old cluster and PVCs until the new cluster has passed the full
  validation window.

## Completion checklist

- [ ] Preflight has no failures.
- [ ] Full `repo2` backup label and object path are recorded.
- [ ] Isolated restore drill passed.
- [ ] Source manifests and Helm values were exported.
- [ ] Applications were quiesced for the final backup/cutover.
- [ ] Operator and `PerconaPGCluster` are ready.
- [ ] Primary, replicas, and PgBouncer are healthy.
- [ ] Database and application integrity checks passed.
- [ ] Scheduled backup and monitoring checks passed after the change.
- [ ] Old resources were preserved through the validation window.
