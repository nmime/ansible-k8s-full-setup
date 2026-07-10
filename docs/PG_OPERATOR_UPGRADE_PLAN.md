# Percona PG Operator Major Upgrade Plan: 2.x → 3.x

## Summary

| Item                | Current                     | Target                       |
|---------------------|-----------------------------|------------------------------|
| PG Operator         | `percona/pg-operator:2.8.2` | `percona/pg-operator:3.0.0`  |
| Helm chart          | `percona/pg-operator 2.8.2` | `percona/pg-operator 3.0.0`  |
| PostgreSQL          | `18`                        | `18` (unchanged)             |
| Cluster CRD         | `postgresql.percona.com/v1` | `postgresql.percona.com/v2`  |
| Backup provider     | pgBackRest                  | pgBackRest (unchanged)       |
| Proxy               | PgBouncer (integrated)      | PgBouncer (standalone CR)    |

> **CRITICAL WARNING:** Percona PG Operator 3.x is a **complete rewrite** of the operator.
> The CRD API changes from `postgresql.percona.com/v1` to `postgresql.percona.com/v2`,
> the `PostgresCluster` spec is significantly restructured, and **in-place upgrades are
> NOT supported**. The only supported path is to **recreate the cluster from a pgBackRest
> backup** under the new operator.

---

## 1. Why PG Operator 2 → 3 Requires Cluster Recreation

### Breaking Changes

| Area                | 2.x (current)                            | 3.x (target)                            |
|---------------------|------------------------------------------|-----------------------------------------|
| CRD API version     | `postgresql.percona.com/v1`              | `postgresql.percona.com/v2`             |
| CRD structure       | Flat spec with inline PgBouncer          | Modular spec; PgBouncer is separate CR  |
| Instance naming     | `pgcluster-N`                            | `pgcluster-instance1-N`                 |
| Pod labels          | `cluster-name=<name>`                    | `percona.com/cluster=<name>`            |
| Backup CR           | `PerconaPGBackup` (v1)                   | `PerconaPGBackup` (v2, new spec fields) |
| Volume layout       | `/pgdata` inside container               | `/pgdata` with subpath for replicas     |
| Operator deployment | Single deployment                        | Multi-component (operator + webhooks)   |
| RBAC / CRDs         | v1 CRDs only                             | v1 + v2 CRDs; v1 resources orphaned    |

### Incompatibility Summary

1. **CRD version bump**: `v1` → `v2` — Kubernetes does not auto-convert existing `PostgresCluster` resources.
2. **Spec restructuring**: The `PostgresCluster` spec fields are reorganised. A 2.x spec **cannot be applied** as-is under operator 3.x.
3. **PgBouncer decoupling**: PgBouncer is no longer configured inside the `PostgresCluster` spec. It requires a separate `PgBouncer` CR.
4. **Label selector changes**: Pods are labelled differently, so existing Services/Endpoints will not match new pods automatically.
5. **Backup CR spec changes**: The `PerconaPGBackup` CR has new required fields; old-style backups may not be directly usable for restore.

### The Only Supported Path: Restore from pgBackRest

```
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  PHASE 1: BACKUP     │────▶│  PHASE 2: STAGING    │────▶│  PHASE 3: DEPLOY     │
│                      │     │                      │     │                      │
│ • Final full backup  │     │ • Deploy operator 3x │     │ • Deploy operator 3x │
│ • Verify S3          │     │ • Restore from S3    │     │   in production NS   │
│ • Record cluster spec│     │ • Verify data        │     │ • Create v2 cluster  │
│                      │     │                      │     │ • Restore from S3    │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
                                                                 │
┌──────────────────────┐     ┌──────────────────────┐     ┌──────────────────────┐
│  PHASE 5:            │◀────│  PHASE 4: CUTOVER    │◀────│  (continue)          │
│  DECOMMISSION        │     │                      │     │ • Wait for ready     │
│                      │     │ • DNS / svc cutover  │     │ • Validate replicas  │
│ • Scale down old     │     │ • PgBouncer migrate  │     │                      │
│ • Delete old PVCs    │     │ • Smoke-test apps    │     │                      │
└──────────────────────┘     └──────────────────────┘     └──────────────────────┘
```

---

## 2. Prerequisites

### 2.1 pgBackRest Full Backup

A **full** pgBackRest backup must exist and be verified before starting the upgrade.

```bash
# Trigger a full backup on the current 2.x cluster
kubectl exec -n databases -c pgbackrest -- pgbackrest --type=full backup

# Verify the backup completed
kubectl logs -n databases -l percona.com/cluster=postgres-operator --tail=200 \
  | grep -i "pgbackrest\|backup"

# Confirm backup artefacts in S3
aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" \
  s3 ls "s3://pgbackrest-backups/backup/" --recursive
```

### 2.2 Replica Lag

All replicas must be in sync before backup. Any lag means data loss for unreplicated transactions.

```bash
kubectl exec -n databases postgres-operator-0 -- psql -U postgres -c "
  SELECT client_addr, state,
    pg_wal_lsn_diff(sent_lsn, replay_lsn) AS lag_bytes
  FROM pg_stat_replication;"
```

### 2.3 S3 / Object Storage Access

The pgBackRest repository must be reachable from both the old cluster (for the backup) and the
new cluster (for the restore). Verify:

```bash
# Test S3 connectivity from within the cluster
kubectl run -n databases s3-test --rm -i --tty --restart=Never \
  --image=amazon/aws-cli:alpine -- \
  aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 ls "s3://pgbackrest-backups/"
```

### 2.4 Disk Space

Ensure at least **2× current data size** is available for the restore. The pgBackRest restore process
needs temporary space to extract WAL segments.

### 2.5 pgBouncer Connection Inventory

Document every service/application connecting to PostgreSQL through PgBouncer so they can be
updated or re-targeted after cutover.

```bash
kubectl get svc -n databases -l percona.com/cluster=postgres-operator -o wide
kubectl get endpoints -n databases -l percona.com/cluster=postgres-operator
```

### 2.6 Preflight Script

Run the automated preflight check before starting:

```bash
./scripts/pg-upgrade-check.sh
```

All checks must report **PASS**.

### 2.7 Restore Drill

Validate the restore path in an isolated namespace:

```bash
./scripts/pg-restore-drill.sh --dry-run     # plan review
./scripts/pg-restore-drill.sh               # execute drill
```

---

## 3. Migration Procedure

### Phase 1 — Backup

1. **Quiesce writes** — put applications in maintenance mode or set `default_transaction_read_only = on`.
2. **Take final pgBackRest full backup** (see §2.1).
3. **Verify backup in S3** — list backup files, confirm size is non-zero.
4. **Export current cluster spec**:

   ```bash
   kubectl get postgrescluster -n databases -o yaml \
     > /tmp/pg-cluster-spec-v1-backup.yaml
   kubectl get pgbouncer -n databases -o yaml \
     > /tmp/pgbouncer-spec-backup.yaml
   ```

### Phase 2 — Staging (Parallel Validation)

1. **Create isolated namespace**: `pg-operator-upgrade-staging`.
2. **Deploy PG Operator 3.0** into the staging namespace (via Helm).
3. **Create v2 `PostgresCluster`** with `restore` source pointing to pgBackRest S3 repository.
4. **Wait for restore** to complete and cluster to be Ready.
5. **Verify data integrity**: database list, table counts, extension versions, replica lag.
6. **Verify PgBouncer** connectivity through the new cluster.
7. **Tear down** the staging namespace.

### Phase 3 — Deploy (Production)

1. **Install PG Operator 3.0** into the production `databases` namespace.
   > NOTE: Do **not** modify `roles/k8s-databases/tasks/main.yml`. Override `pg_operator_ver`
   > via Ansible extra-vars or Helm directly for this one-time operation.
2. **Delete v1 PostgresCluster** — cascade=orphan to preserve PVCs:
   ```bash
   kubectl delete postgrescluster postgres-operator -n databases --cascade=orphan
   ```
3. **Delete old PVCs** (the restored cluster will provision new ones):
   ```bash
   kubectl delete pvc -n databases -l percona.com/cluster=postgres-operator
   ```
4. **Create v2 PostgresCluster** with pgBackRest restore source.
5. **Create PgBouncer CR** with updated instance references.
6. **Wait for readiness** of all pods, replicas, and PgBouncer.

### Phase 4 — Cutover

1. **Verify service names** — the PgBouncer service may have a new name under operator 3.x.
   If so, update application connection strings or create a Service alias.
2. **Smoke-test** every application that writes to PostgreSQL.
3. **Monitor** replica lag and PgBouncer connection counts for 30 minutes.

### Phase 5 — Decommission

1. Keep the **old cluster** running in read-only mode for **24 hours** as a safety net.
2. After 24 hours with no issues:
   - Scale down old cluster replicas to 0
   - Delete old PVCs
   - Delete old services and ConfigMaps

### PgBouncer Migration Detail

| Setting           | 2.x Location                     | 3.x Location                    |
|-------------------|----------------------------------|---------------------------------|
| Pool mode         | `PostgresCluster.spec.proxy`     | `PgBouncer.spec.poolMode`       |
| Max connections   | `PostgresCluster.spec.proxy.maxConnections` | `PgBouncer.spec.maxClientConn` |
| Auth query        | Inline in PgBouncer config       | `PgBouncer.spec.authQuery`      |

**Migration steps**:

```bash
# 1. Export existing PgBouncer config
kubectl exec -n databases $(kubectl get pod -n databases -l percona.com/component=proxy \
  -o jsonpath='{.items[0].metadata.name}') -- cat /pgbouncer/pgbouncer.ini \
  > /tmp/pgbouncer-config-backup.ini

# 2. After new PgBouncer CR is up, verify config
kubectl exec -n databases $(kubectl get pod -n databases -l percona.com/component=proxy \
  -o jsonpath='{.items[0].metadata.name}') -- cat /pgbouncer/pgbouncer.ini

# 3. Diff the configs
diff /tmp/pgbouncer-config-backup.ini <(kubectl exec -n databases ... -- cat /pgbouncer/pgbouncer.ini)
```

---

## 4. Rollback Plan

### 4.1 Rollback Before Phase 5

| Phase | Rollback action |
|-------|----------------|
| Phase 1 (Backup) | Lift maintenance mode; cluster unchanged |
| Phase 2 (Staging) | No action; staging is isolated |
| Phase 3 (Deploy) | Scale down new cluster; restore v1 cluster spec from backup; old PVCs still exist |
| Phase 4 (Cutover) | Revert PgBouncer DNS/Service to old cluster; old cluster is still running |

```bash
# Rollback: restore v1 cluster from saved spec
kubectl apply -f /tmp/pg-cluster-spec-v1-backup.yaml
kubectl apply -f /tmp/pgbouncer-spec-backup.yaml
```

### 4.2 Emergency Restore (After Decommission)

If the old cluster was already decommissioned and a problem is discovered:

1. Use the pgBackRest full backup from Phase 1.
2. Deploy PG Operator 2.x or 3.x (whichever is needed).
3. Create a new cluster with pgBackRest restore source.

```bash
./scripts/pg-restore-drill.sh --namespace pg-cluster-recovery
```

---

## 5. Risk Assessment

| Risk                              | Severity | Mitigation                                    |
|-----------------------------------|----------|-----------------------------------------------|
| Data loss during migration        | CRITICAL | Full backup verified; staging restore validated |
| Extended downtime (>15 min)       | HIGH     | Restore drill run in advance                  |
| pgBackRest restore failure        | HIGH     | Staging phase catches issues                  |
| PgBouncer endpoint mismatch       | MEDIUM   | Pre-deployment DNS audit                      |
| Application connection failures   | MEDIUM   | Smoke tests in cutover phase                  |
| S3 credentials misconfigured      | HIGH     | S3 connectivity test in preflight             |
| Insufficient disk space           | MEDIUM   | Disk space check in preflight script          |
| pgBackRest restore fails on drill | LOW      | Dry-run restore drill catches issues early    |

---

## 6. Checklist

### Pre-Migration

- [ ] Run `./scripts/pg-upgrade-check.sh` — all checks PASS
- [ ] Full pgBackRest backup exists and is verified in S3
- [ ] Replica lag is zero across all replicas
- [ ] S3 connectivity confirmed from cluster
- [ ] Disk space ≥ 2× current data size
- [ ] Restore drill succeeded (`./scripts/pg-restore-drill.sh`)
- [ ] Application connection inventory documented
- [ ] Current cluster spec exported (`/tmp/pg-cluster-spec-v1-backup.yaml`)
- [ ] PgBouncer config exported (`/tmp/pgbouncer-config-backup.ini`)
- [ ] Maintenance window communicated

### During Migration

- [ ] Applications in maintenance mode
- [ ] Final pgBackRest backup taken
- [ ] PG Operator 3.0 installed in production namespace
- [ ] v1 PostgresCluster deleted (cascade=orphan)
- [ ] v2 PostgresCluster created with restore source
- [ ] Restore verified: databases, tables, extensions match
- [ ] PgBouncer CR created and pods healthy
- [ ] Application connectivity verified
- [ ] Replication healthy on all replicas

### Post-Migration

- [ ] Old cluster running read-only for 24h safety window
- [ ] No application errors observed
- [ ] Old cluster decommissioned after 24h
- [ ] pgBackRest backup schedule re-verified
- [ ] Monitoring/alerting confirmed for new cluster
- [ ] Stakeholders notified of successful migration

---

## 7. Communication Plan

| Time                        | Audience          | Channel              | Message                        |
|-----------------------------|-------------------|----------------------|--------------------------------|
| 48h before window           | All stakeholders  | Email + Slack        | Maintenance date, expected downtime |
| Start of window             | All stakeholders  | Slack + status page  | Maintenance started            |
| Restore complete            | Engineering       | Slack                | Cluster restored, validating   |
| Cutover complete            | All stakeholders  | Slack + status page  | Service restored               |
| 24h post-migration          | All stakeholders  | Email                | Migration successful           |
| On rollback                 | All stakeholders  | Slack + PagerDuty    | Rollback in progress           |
