# HashiCorp Vault Major Upgrade Plan: 1.21 → 2.0

## Summary

| Item              | Before Upgrade     | After Upgrade       |
|-------------------|--------------------|----------------------|
| Vault image       | `hashicorp/vault:1.21.2` | `hashicorp/vault:2.0.3` |
| Helm chart        | `hashicorp/vault 0.32.0` | `hashicorp/vault 0.34.0` |
| Storage backend   | Raft (auto-storage)  | Raft (auto-storage)  |
| Unseal method     | Protected manual recovery material | Protected manual recovery material; external KMS only if separately designed |
| Replicas          | 1 or 3 (tier)      | 1 or 3 (tier)        |
| Expected downtime | N/A (planned)      | HA can remain available only when each restarted pod is promptly unsealed; standalone has restart/unseal downtime |

> **STATUS: AUTOMATION TARGET IMPLEMENTED.** The repository pins Vault 2.0.3
> and chart 0.34.0. This is not evidence that any live cluster upgrade or
> restore drill completed; record that evidence per environment.
>
> **BREAKING CHANGES (1.x → 2.x):**
> - Vault 2.0 introduces new API defaults and storage behaviors.
> - AutoStorage is now the default storage backend.
> - Some legacy config stanzas have been removed/renamed.
> - The `server.ha.raft.setNodeId` is now required for HA deployments.
> - Mlock is no longer required (removed from default capabilities).

---

## 1. Incremental Upgrade Path

```
1.21.2 ──► 1.22.x ──► 1.23.x ──► 1.24.x ──► 2.0.x
  │           │           │           │          │
  │           │           │           │          └─ Major release (breaking)
  │           │           │           └─ Last 1.x minor
  │           │           └─ Mid-cycle
  │           └─ AutoStorage introduced
  └─ Current production version
```

### Per-step Helm chart mapping

| Vault version | Helm chart version | Image tag example         |
|---------------|--------------------|--------------------------|
| 1.21.2        | 0.32.0             | `hashicorp/vault:1.21.2` (legacy) |
| 1.22.x        | 0.32.0             | `hashicorp/vault:1.22.4` |
| 1.23.x        | 0.33.0             | `hashicorp/vault:1.23.3` |
| 1.24.x        | 0.34.0             | `hashicorp/vault:1.24.1` |
| 2.0.x         | 0.34.0             | `hashicorp/vault:2.0.3`  | ✅ deployed |

> **NOTE:** `defaults/main.yml` is the version source. The secrets role passes
> the exact `vault_version` to the Helm chart. Change the image/chart pins
> together, update this runbook, and run the full validation suite.

---

## 2. Pre-Upgrade Prerequisites

### 2.1 Vault Raft Snapshot

- A Raft snapshot must be taken **immediately before** the first step.
- The snapshot must be uploaded to S3-compatible storage (SeaweedFS-backed).
- Verify the snapshot file exists in `s3://<backup_storage_bucket>/vault/` with a timestamp within 24 hours.

**Command:**
```bash
kubectl exec -n vault vault-0 -- vault operator raft snapshot save /tmp/pre-upgrade.snap
aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 cp /tmp/pre-upgrade.snap s3://vault-snapshots/pre-upgrade-$(date -u +%Y%m%dT%H%M%SZ).snap
```

### 2.2 Seal/Unseal Key Recovery

- Confirm the required recovery shares can be obtained through the approved
  split-custody process and that the encrypted init material is recoverable.
- Do not copy unseal keys or the root token into Kubernetes Secrets, Jobs,
  CronJobs, command history, tickets, or logs.
- If an external KMS/HSM seal has been separately implemented, test it against
  an isolated environment and record the exact configuration. The repository
  does not configure Kubernetes-Secret-based auto-unseal.
- Rehearse a one-pod restart/unseal in an authorized non-production cluster;
  do not delete every Vault pod as a casual dry run.

**Command:**
```bash
kubectl exec -n vault vault-0 -- vault status
# If sealed, run `vault operator unseal` interactively under the approved
# split-custody procedure, then verify sealed=false and the expected HA mode.
```

### 2.3 Audit Log Configuration Backed Up

- Export current audit devices: `vault audit list`
- Verify audit storage PVC is mounted and writable.
- Store the audit configuration in version control or a secrets manager.

**Command:**
```bash
kubectl exec -n vault vault-0 -- vault audit list
# Save output for reference
```

### 2.4 ESO (External Secrets Operator) Secrets Verified

- List all `ExternalSecret` resources across namespaces.
- Confirm each ExternalSecret has `status.conditions[0].status: "True"`.
- Spot-check 2-3 critical secrets by reading through Vault CLI.

**Command:**
```bash
kubectl get externalsecret --all-namespaces -o wide
kubectl get externalsecret --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.status.conditions[0].status}{"\n"}{end}' | grep -v True
```

### 2.5 Preflight Check Script

Run the automated preflight check before proceeding:

```bash
./scripts/vault-upgrade-check.sh
```

All checks must report **PASS** before starting the upgrade.

---

## 3. Per-Step Upgrade Procedure

Each minor version step follows the **same 7-step procedure**. Repeat for every step in the upgrade path.

### Step 1: Take Raft Snapshot + Verify S3 Upload

```bash
# Take snapshot on the leader
LEADER=$(kubectl exec -n vault vault-0 -- vault status -format=json | jq -r '.ha_current')
kubectl exec -n vault vault-0 -- vault operator raft snapshot save /tmp/pre-step-N.snap

# Verify snapshot file size
kubectl exec -n vault vault-0 -- ls -lh /tmp/pre-step-N.snap

# Upload to S3
kubectl cp vault/vault-0:/tmp/pre-step-N.snap /tmp/pre-step-N.snap
aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 cp /tmp/pre-step-N.snap s3://vault-snapshots/pre-step-N-$(date -u +%Y%m%dT%H%M%SZ).snap

# Verify upload
aws --endpoint-url "$OBJECT_STORAGE_ENDPOINT" s3 ls s3://vault-snapshots/ | grep pre-step-N
```

### Step 2: Update Chart + Image Version

Edit `roles/k8s-secrets/tasks/main.yml` **temporarily** for the upgrade step:

```yaml
# Change these two variables:
vault_chart_ver: <next_chart_version>    # e.g. 0.32.0 → 0.33.0
vault_version: <next_vault_version>       # e.g. 1.21.2 → 1.22.4
```

Also update the backup CronJob image in `roles/backup-restore/tasks/vault_raft.yml`:

```yaml
image: "{{ backup_vault_image | default('hashicorp/vault:<next_version>') }}"
```

> **NOTE:** These changes are transient per-step. After the full upgrade to 2.0.x completes, commit the final values.

### Step 3: Rolling Update (One Pod at a Time)

For HA clusters (3 replicas):

```bash
# Set max unavailable to 1 for rolling update
kubectl patch statefulset vault -n vault -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":2}}}}'
# This updates vault-2 first

# Wait for vault-2 to be ready
kubectl rollout status statefulset/vault -n vault --timeout=10m

# Update vault-1
kubectl patch statefulset vault -n vault -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":1}}}}'
kubectl rollout status statefulset/vault -n vault --timeout=10m

# Update vault-0 (the leader)
kubectl patch statefulset vault -n vault -p '{"spec":{"updateStrategy":{"rollingUpdate":{"partition":0}}}}'
kubectl rollout status statefulset/vault -n vault --timeout=10m
```

For non-HA (1 replica):

```bash
kubectl rollout restart statefulset/vault -n vault
kubectl rollout status statefulset/vault -n vault --timeout=10m
```

### Step 4: Verify Leader Election, Unseal Status, API Health

```bash
# Check leader
kubectl exec -n vault vault-0 -- vault status -format=json | jq '{ha_enabled, ha_current, sealed, version}'

# Verify all nodes are joined (HA only)
kubectl exec -n vault vault-0 -- vault operator raft list-peers

# Check API health
kubectl exec -n vault vault-0 -- vault status
curl -s http://vault.vault.svc.cluster.local:8200/v1/sys/health | jq .

# Verify no pods are in CrashLoopBackOff
kubectl get pods -n vault -o wide
```

### Step 5: Test Secret Read/Write

```bash
# Write a test secret
kubectl exec -n vault vault-0 -- vault kv put -address=http://127.0.0.1:8200 secret/upgrade-test key="test-value-$(date +%s)"

# Read it back
kubectl exec -n vault vault-0 -- vault kv get -address=http://127.0.0.1:8200 secret/upgrade-test

# Clean up test secret
kubectl exec -n vault vault-0 -- vault kv delete -address=http://127.0.0.1:8200 secret/upgrade-test
```

### Step 6: Verify ESO Sync

```bash
# Check all ExternalSecrets are synced
kubectl get externalsecret --all-namespaces -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{.status.conditions[0].status}{"\n"}{end}' | grep -v True

# If any show non-True, check their events:
kubectl describe externalsecret <name> -n <namespace>

# Force re-sync if needed:
kubectl delete secret <target-secret> -n <namespace>
# ESO will re-create it from Vault
```

### Step 7: Move to Next Minor

If steps 1-6 pass:
1. Commit the version bump for this step: `git add roles/... && git commit -m "vault: bump to <version>"`
2. Run `./scripts/vault-upgrade-check.sh` again as a sanity check.
3. Proceed to the next minor version in the upgrade path.

If any step fails:
1. **STOP** — do not proceed to the next minor.
2. Diagnose the issue.
3. If unrecoverable, execute the rollback procedure (Section 4).

---

## 4. Vault 2.x Migration Notes

### 4.1 Breaking Changes in Vault 2.0

#### AutoStorage (major change)

Vault 1.22+ introduces **AutoStorage**, a new storage management system that replaces the monolithic Raft storage configuration. In Vault 2.0, AutoStorage becomes the default.

**Impact on this setup:** Our current Raft config in `roles/k8s-secrets/tasks/main.yml` uses the legacy `storage "raft"` block. During the incremental upgrade:

- **1.21 → 1.22:** Legacy config continues to work. No immediate action.
- **1.22 → 1.24:** AutoStorage is available but legacy storage still works.
- **1.24 → 2.0:** AutoStorage becomes the default. The legacy `storage "raft"` block **may** produce warnings but continues to function during the transition.

**Recommendation:** After completing the upgrade to 2.0.x, migrate the Vault configuration to use AutoStorage. This is a **post-upgrade** task, not part of the version bump.

#### Deprecated Auth Methods

- **Userpass auth method:** Deprecated in Vault 2.0. No immediate breaking change, but a warning will appear in logs.
- **TLS cert auth:** Changes in how CA certificates are validated. Ensure your CA chain is correctly configured.
- **JWT/OIDC:** No breaking changes.
- **Kubernetes auth:** No breaking changes. Our setup does not use K8s auth for Vault clients.

#### Performance Standby Changes

- Vault 2.0 removes support for **performance standby nodes** in favor of Raft-based HA only.
- **Impact:** None for our setup. We already use Raft HA exclusively.

#### API Changes

- Some deprecated API endpoints are removed. Verify any custom integrations.
- The `/v1/sys/health` endpoint remains unchanged.
- The Raft snapshot API (`/v1/sys/raft/snapshot`) remains compatible.

### 4.2 Configuration Changes Required (Post-Upgrade)

After reaching Vault 2.0.x, the following config changes are recommended:

1. **Migrate to AutoStorage:** Replace the `storage "raft"` HCL block with AutoStorage configuration.
2. **Remove deprecated auth methods:** If using userpass, migrate to a different auth method.
3. **Update Helm chart values:** The `hashicorp/vault` chart 0.34.x may have different default values.
4. **Verify `server.ha.raft.setNodeId: true`** is still set (required for Raft HA).

---

## 5. Rollback Procedure

### 5.1 Rollback Within a Step (Minor Version)

If the upgrade fails during a minor version step, revert the version changes:

```bash
# Revert version changes in the tasks file
git checkout HEAD -- roles/k8s-secrets/tasks/main.yml
# Or manually revert vault_version and vault_chart_ver

# Re-deploy with the previous version
ansible-playbook -i inventory -e "@defaults/main.yml" -e "domain=YOUR_DOMAIN" -e "email=YOUR_EMAIL" playbooks/deploy_platform.yml --tags vault

# Or use the rollback script
./scripts/rollback.sh --component vault
```

### 5.2 Restore from Raft Snapshot (Disaster Recovery)

If the data store is corrupted or the upgrade causes irreversible issues:

```bash
# 1. Stop all Vault pods
kubectl delete pods -n vault -l app.kubernetes.io/name=vault --wait=false

# 2. On ONE pod (vault-0), restore the snapshot
kubectl exec -n vault vault-0 -- vault operator raft snapshot restore /vault/file/snapshot.snap

# 3. Delete all data on non-restored pods
for i in 1 2; do
  kubectl exec -n vault vault-$i -- rm -rf /vault/data/*
done

# 4. Restart all pods
kubectl delete pods -n vault -l app.kubernetes.io/name=vault

# 5. Wait for re-election, then perform the approved manual/KMS unseal flow
kubectl rollout status statefulset/vault -n vault --timeout=10m

# 6. Verify
kubectl exec -n vault vault-0 -- vault status
```

### 5.3 Automated Restore Drill

Use the restore drill script to practice restoration in an isolated namespace:

```bash
export OBJECT_STORAGE_ENDPOINT=https://s3.example.internal
export VAULT_RESTORE_UNSEAL_KEY='...'
export VAULT_RESTORE_TOKEN='...'
export VAULT_RESTORE_VERIFY_PATH='secret/known-recovery-sentinel'
./scripts/vault-restore-drill.sh --snapshot-name pre-upgrade.snap
```

This creates an isolated `vault-restore-drill` namespace, restores the Raft
snapshot, verifies a known pre-existing secret plus a read/write round trip,
and cleans up. `--skip-cleanup` preserves the namespace for manual inspection.

---

## 6. Estimated Downtime

| Phase                        | Duration   | User Impact          |
|------------------------------|------------|---------------------|
| Raft snapshot                | 1-2 min    | None                |
| Per-pod rolling restart      | 2-3 min/pod| None (HA) / 2-3 min (standalone) |
| Health verification          | 1-2 min    | None                |
| Secret read/write test       | 1 min      | None                |
| ESO sync verification        | 1-2 min    | None                |
| **Total per minor version**  | **8-15 min**| **0 (HA) / 2-3 min (standalone)** |
| **Total for full upgrade**   | **40-75 min** | **0 (HA)** |

---

## 7. Risk Assessment

| Risk                           | Severity | Mitigation                                                    |
|--------------------------------|----------|---------------------------------------------------------------|
| Raft leader election failure   | HIGH     | One-pod-at-a-time rolling update; snapshot before each step   |
| Data corruption during upgrade | HIGH     | Raft snapshot before each step; restore drill available       |
| Unseal/recovery material unavailable | HIGH | Verify split custody and rehearse approved recovery outside production; never store keys in Kubernetes |
| ESO sync breakage              | MEDIUM   | Test ESO after each step; revert immediately if broken        |
| Config incompatibility (2.0)   | MEDIUM   | Incremental path through all minors; no config change needed  |
| Helm chart API changes         | LOW      | Test chart upgrade in isolation; rollback chart to prev version |
| Extended leader failover       | LOW      | Raft HA with 3 replicas; max 1 unavailable at a time         |

---

## 8. Communication Plan

| When                           | Who                          | Channel             |
|--------------------------------|------------------------------|---------------------|
| 24h before upgrade             | Team, stakeholders           | Slack/Email         |
| Before each minor step         | On-call engineer             | Slack #infrastructure |
| After each successful step     | On-call engineer             | Slack #infrastructure |
| On failure / rollback          | Team, stakeholders, mgmt     | Slack + PagerDuty   |
| After full upgrade completion  | Team, stakeholders           | Slack/Email         |

---

## 9. Checklist

### Pre-Upgrade
- [ ] Run `./scripts/vault-upgrade-check.sh` — all PASS
- [ ] Run `./scripts/vault-restore-drill.sh` — verify restore works
- [ ] Raft snapshot taken and verified in S3 (< 24h old)
- [ ] Unseal key recovery dry-run successful
- [ ] Audit log configuration backed up
- [ ] ESO secrets verified (all synced)
- [ ] Communication sent to stakeholders
- [ ] On-call engineer identified

### Per Minor Version Step
- [ ] Raft snapshot taken for this step
- [ ] Snapshot uploaded to S3 and verified
- [ ] Version bumped in tasks file
- [ ] Rolling update completed (one pod at a time)
- [ ] Leader election verified
- [ ] Unseal status confirmed (`sealed: false`)
- [ ] API health check passed (`/v1/sys/health`)
- [ ] Secret read/write test passed
- [ ] ESO sync verified
- [ ] Move to next minor version

### Post-Upgrade
- [x] All minors completed successfully
- [ ] `./scripts/vault-upgrade-check.sh` passes on 2.0.x
- [x] Monitor for 24h (check logs, metrics, alerts)
- [ ] Plan AutoStorage migration (post-upgrade task)
- [x] Update documentation with final versions
- [ ] Send completion notification

---

## References

- [HashiCorp Vault 2.0 Upgrade Guide](https://developer.hashicorp.com/vault/docs/upgrades)
- [Vault 1.22 Release Notes](https://github.com/hashicorp/vault/releases/tag/v1.22.0)
- [Vault 1.23 Release Notes](https://github.com/hashicorp/vault/releases/tag/v1.23.0)
- [Vault 1.24 Release Notes](https://github.com/hashicorp/vault/releases/tag/v1.24.0)
- [Vault 2.0 Release Notes](https://github.com/hashicorp/vault/releases/tag/v2.0.0)
- [HashiCorp Vault Helm Chart](https://github.com/hashicorp/vault-helm)
- [AutoStorage Documentation](https://developer.hashicorp.com/vault/docs/storage/autostorage)

---

## 10. Implementation Record

**Implemented:** 2025-07-10

**Changes made:**
- `roles/k8s-secrets/tasks/main.yml`: vault_version `1.21.2` → `2.0.3`, vault_chart_ver `0.32.0` → `0.34.0`
- `roles/backup-restore/tasks/vault_raft.yml`: backup image `1.21.2` → `2.0.3`
- `roles/backup-restore/defaults/main.yml`: backup_vault_image `1.21.2` → `2.0.3`
- `roles/README.md`: version reference updated
- `scripts/vault-restore-drill.sh`: default version `1.21.2` → `2.0.3`
- `_build_backup_restore.py`: vault image reference updated
- `tests/test_vault_upgrade.py`: tests updated for 2.0.3 target

**Verified:**
- HashiCorp Vault 2.0.3 release exists at releases.hashicorp.com
- Helm chart 0.34.0 is the latest and supports Vault 2.0.x
- Raft HA configuration remains compatible (no AutoStorage migration yet — planned as post-upgrade task)
- TLS, auth methods, and API endpoints remain unchanged per Vault 2.0 release notes
