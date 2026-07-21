# Five-Tier Live Test Report — 2026-07-21

## Result and scope

All five named profiles were deployed concurrently as isolated Hetzner Cloud
clusters and exercised against delegated `n0xeid.xyz` DNS. The accepted evidence
set contains 23/23 Ready Kubernetes nodes, 141/141 protected PVCs, five passing
profile-aware smoke runs, five healthy final snapshots, five complete encrypted
cluster bundles, five successful local archive-verification runs, and 254,950
load operations with zero reported errors. All five production component drill
paths also passed: GitLab archive, PostgreSQL, Vault, SeaweedFS, and MongoDB.

This report describes the disposable `load5-260720-r5` campaign. It does not
convert a successful backup verification into a claim that every application
has been restored. The exact restore and cleanup boundaries are recorded below.
Credential values, decrypted recovery material, provider identifiers, and
Secret contents are deliberately excluded.

## Profiles and technology intent

All profiles include the managed Hetzner network/private-node foundation,
Headscale access, Kubernetes 1.35.4, containerd, Cilium/Hubble and Gateway API,
cert-manager, Hetzner CCM/CSI, MetalLB, SeaweedFS, Vault, Percona PostgreSQL,
Argo CD, the VictoriaMetrics/Grafana/logging core, and KEDA. The named-profile
differences exercised by this campaign were:

| Profile | Capability/resource tier | Kubernetes topology | Named-profile selectable technologies |
|---|---|---|---|
| `minimal` | `minimal` / `minimal` | 1 schedulable control plane + 1 worker; no provider LB | Common set only; ESO, MongoDB, Elasticsearch, Dragonfly, GitLab/Runner, PMM, Coroot, tracing, Temporal, Postal, GlitchTip, APM, and Blackbox off |
| `small` | `small` / `small` | 1 dedicated control plane + 2 workers | Common set plus Dragonfly and GitLab/Runner; full-platform services remain off |
| `medium` | `medium` / `medium` | 3 schedulable control planes + 2 workers | Full set: ESO, MongoDB, Elasticsearch/Kibana, Dragonfly, GitLab/Runner, PMM, Coroot, Tempo/OpenTelemetry, Temporal, Postal, GlitchTip, Elastic APM, Blackbox, native backups, and external DR |
| `medium-optimized` | `medium` / `small` | 3 schedulable control planes + 4 workers | Same full technology set as `medium`, with conservative requests, retention, replicas, and autoscaling; quorum services remain replicated |
| `production` | `production` / `small` | 3 dedicated control planes + 3 workers | Full technology set with explicit HA replicas and failover headroom, including two-replica VictoriaMetrics storage at replication factor 2 |

Daytona and HIPAA-oriented hardening are opt-in and were off in all five named
profiles. To test recovery consistently, the campaign explicitly enabled native
backup and external disaster recovery on `minimal` and `small`; this was a
campaign selection override, not a change to their named-profile defaults.
`--minimum-storage` preserved technology selection, node topology, and replica
intent while reducing profile-controlled persistent requests to the provider's
10 GiB test minimum, colocating SeaweedFS indexes on durable data claims, and
using pod-local GitLab backup staging. The completed backup artifacts remained
durable in external S3.

The `minimal` cluster was safely resized one node at a time to `cpx32` for both
nodes after live testing showed that 2-vCPU/4-GiB nodes could not keep the core
stack plus one Velero node agent per node schedulable. The profile floor is now
4 vCPU/8 GiB. This did not change its two-node topology or technology set.

The authoritative selectable-component matrix, dependencies, and later
enable/disable/remove behavior remain in
[`TECHNOLOGY_CATALOG.md`](TECHNOLOGY_CATALOG.md).

## Final cluster evidence

The final snapshots were collected after load with
`scripts/collect-live-evidence.sh`. Every snapshot reported `healthy: true`, no
node pressure, no unready pod, no unavailable Deployment/StatefulSet/DaemonSet,
no failed Job, no unbound PVC, no unavailable APIService, and no unready
certificate or HTTPRoute. Provider-edge checks were required and healthy for
all profiles except `minimal`, whose declared topology intentionally has no
Hetzner load balancer and uses bastion ingress.

| Profile | Nodes Ready | Pods observed | Provider edge | PVCs evaluated/protected | PVC failures |
|---|---:|---:|---|---:|---:|
| `minimal` | 2/2 | 94 | Not required | 11/11 | 0 |
| `small` | 3/3 | 126 | Healthy, 4/4 checks | 13/13 | 0 |
| `medium` | 5/5 | 192 | Healthy, 4/4 checks | 40/40 | 0 |
| `medium-optimized` | 7/7 | 180 | Healthy, 8/8 checks | 37/37 | 0 |
| `production` | 6/6 | 209 | Healthy, 6/6 checks | 40/40 | 0 |
| **Total** | **23/23** | **801** | **All required edges healthy** | **141/141** | **0** |

Each profile's smoke run passed S3 write/read/delete, PostgreSQL write/read,
Vault KV, KEDA, observability, GitOps, and all selected Gateway routes. The
route probe accepts HTTP 426 from GitLab KAS because KAS is a WebSocket-only
endpoint; normal TLS validation remained enabled.

After the component drills and intentional GitLab cleanup, a new production
`post-restore` snapshot reported `healthy: true`: 6/6 nodes Ready, zero node
pressure, 195 observed pods with none pending/failed/unready, zero unavailable
controllers, zero failed Jobs, zero unbound PVCs, healthy certificates/routes/
APIServices, and 6/6 healthy provider-edge checks. This supersedes the earlier
production snapshot as the final live-health result.

## Bounded load result

The five controllers ran concurrently; phases inside each cluster were ordered
and cleanup-gated. Every enabled phase passed with `max_error_percent: 0` and
`max_restart_delta: 0`. Dragonfly is not selected by `minimal` and was therefore
correctly skipped there.

| Profile | HTTP | S3 | PostgreSQL | Vault | Dragonfly | Total | Errors |
|---|---:|---:|---:|---:|---:|---:|---:|
| `minimal` | 500 | 100 | 200 | 150 | skipped | 950 | 0 |
| `small` | 2,000 | 400 | 1,000 | 600 | 10,000 | 14,000 | 0 |
| `medium` | 10,000 | 2,000 | 5,000 | 3,000 | 50,000 | 70,000 | 0 |
| `medium-optimized` | 5,000 | 1,000 | 2,500 | 1,500 | 20,000 | 30,000 | 0 |
| `production` | 20,000 | 4,000 | 10,000 | 6,000 | 100,000 | 140,000 | 0 |
| **Total** | **37,500** | **7,500** | **18,700** | **11,250** | **180,000** | **254,950** | **0** |

Temporary S3 objects, PostgreSQL tables, Vault paths, and Dragonfly keys used by
the harness were removed. The load gate distinguishes actual container restarts
and same-name UID replacement from expected new-name HPA scale events, while
still rejecting nonzero operation errors and unhealthy settled evidence.

## Backup and archive verification

Each tier produced one complete age-encrypted cluster bundle. The bundle
contains desired config, encrypted generated secrets, the still-Ansible-Vault-
encrypted Vault init material, Kubespray inventory, Helm and Kubernetes API
state, an etcd snapshot, control-plane PKI/config, cloud state, and repository
recovery state. Plaintext recovery material is not retained in this report.

For every tier, the local receipt recorded:

- `completeness: complete`;
- external publication complete;
- encrypted archive uploaded before its checksum;
- a successful remote download and SHA-256 comparison;
- the completion receipt uploaded last;
- all inventoried PVCs Bound, mounted, and covered by completed Velero/Kopia
  pod-volume backups.

The initial object layout exposed a Velero-prefix collision: cluster recovery
bundles had been placed beneath the prefix Velero exclusively owns, and the
object-store plugin treats unknown entries there as an invalid backup-store
layout. The default is now the sibling
`<Velero prefix parent>/cluster-bundles/<project>` path. An explicit bundle
prefix equal to or nested below the Velero prefix fails before publication.
Each tier's existing archive/checksum/receipt triplet was migrated to the sibling
layout; the encrypted archive was downloaded and checksum-verified before the
updated receipt was uploaded last. All five local/remote receipt comparisons
and archive verifies passed again after migration, and all five Velero
BackupStorageLocations reported `Available`.

`scripts/cluster-restore.sh --mode verify` then independently decrypted and
verified all five local archives, their external checksums, internal
`SHA256SUMS`, project identity, completeness marker, and required Vault recovery
dependency. This is archive-integrity and recovery-input proof; it does not
mutate a cluster or replay PVC contents.

Production then exercised every isolated component drill:

| Component | Result | Recovery evidence |
|---|---|---|
| GitLab | 5 passed, 0 failed, 0 warnings | Toolbox archive downloaded into an isolated PVC, size-checked, safely extracted with the exact source image, and repository metadata/payload verified |
| PostgreSQL | 15 passed, 0 failed | Exact successful repo2 pgBackRest set restored into an isolated Percona cluster; databases, PostgreSQL 18.4, streaming replication, and connectivity verified |
| Vault | 6 passed, 0 failed | A fresh snapshot containing a known sentinel was restored into an isolated single-node Raft deployment, unsealed, and the restored path plus secret round-trip verified |
| SeaweedFS | passed with 0 restore errors | Exact Kopia snapshot restored into an isolated network-denied replacement PVC; two files and 98,304 bytes matched byte-for-byte |
| MongoDB | passed | Selected PBM backup restored into a disposable Percona MongoDB cluster and verified before cleanup |

Every drill cleaned its isolated namespace. GitLab remains an archive-validation
drill, not a same-version GitLab service restore: this deployment uses external
PostgreSQL and Toolbox runs with `--skip db`. The independently successful exact
pgBackRest drill proves that database recovery path. A full GitLab cutover would
still combine the same-version chart restore, Toolbox archive, retained Rails
secret, external PostgreSQL recovery, and application checks.

## Migration proof

The migration implementation is resumable and supports every ordered transition
among `minimal`, `small`, `medium`, `medium-optimized`, and `production`. The
source test matrix generated and validated all 20 distinct source-to-target
plans. Each plan retains explicit component choices, creates source, target,
expansion, transition, and rollback configs, expands to the maximum source/
target topology before contraction, and records storage/stateful retention.

The workflow includes three backup checkpoints, 3+3 expansion where required,
one-node-at-a-time drain/resize/root-filesystem growth, etcd checks around
control-plane changes, Vault unseal/readiness gates, VictoriaMetrics single ↔
cluster migration, exact Helm/config rollback, dependency-ordered target-only
component removal, Kubespray scale-in, and final unused-resource cleanup.

The automated capacity gate derives persistent source and target claims,
growth, and migration scratch. An offline plan records that quota is required.
Live `execute` refuses to begin without an explicit Hetzner account GiB quota,
combines the estimate with account-wide live volume usage and a safety margin,
persists provider volume IDs/sizes as its baseline, and rechecks quota,
cluster-attributed growth, and plan drift on resume. Unknown storage quantities
fail closed. The representative `minimal` → `production` source test estimated
240 GiB source, 1,310 GiB target, 1,100 GiB target delta, 50 GiB scratch, and
1,250 GiB minimum headroom including the 100 GiB margin.

This campaign proved the planning/state machine and quota boundary through the
automated source suite. It did **not** execute a live all-to-all migration matrix
or a live `minimal` → `production` cutover; those operations would mutate the
already-tested source clusters and require additional temporary provider volume
headroom.

## Source corrections produced by the campaign

Live failures were retained as findings until their causes were fixed and the
affected checks rerun. The resulting source changes include:

- isolated multi-cluster controller state, exact project-label cleanup, and
  explicit minimal-node capacity floors;
- Cilium Gateway API discovery without `pipefail`/SIGPIPE false negatives,
  correct handling of an explicitly disabled provider LB, and live LB target/
  health convergence;
- safe GitLab failed-revision recovery that removes only exact newer failed
  Helm history, plus UID/resource-version/PVC-identity-gated Gitaly StatefulSet
  orphan/reconcile instead of an uninstall or implicit data deletion;
- UID-gated stale GlitchTip Helm revision recovery;
- Vault TLS/Raft/KV/auth reconciliation and fail-closed reuse of encrypted
  initialization state;
- checkpointed, sentinel-verified SeaweedFS index migration and replica
  placement; retained Loki claims and guarded obsolete-PVC cleanup;
- a separate `disaster-recovery` lifecycle selector and migration ordering,
  complete schema-v2 encrypted recovery bundles, atomic remote receipts, and
  exact per-cluster secrets/Vault-init binding;
- exact pgBackRest set restore selection, project-isolated backup logs, bounded
  Vault drill storage, a `Recreate` Vault drill Deployment strategy that avoids
  a ReadWriteOnce-PVC rolling-update deadlock, and truthful GitLab archive-drill
  semantics;
- cluster bundles outside Velero's reserved object prefix, fail-closed prefix
  validation, and safely migrated archive/checksum/final-receipt triplets;
- profile-aware load resources, secure Dragonfly authentication without a
  password argument, exact result accounting, HPA-aware restart detection, and
  final evidence-path preservation on failures;
- persisted schema-v3 migration state, selection retention, volume-capacity
  planning, backup gates, rollback, resumability, and finalization ordering.

## Source validation

The documentation/profile/backup/migration contract lane completed with 353
passing tests across `tests/test_backup_restore.py`,
`tests/test_platform_profiles.py`, and
`tests/test_cluster_disaster_recovery.py`. That run includes the 20 ordered
profile-pair plans and fail-closed volume-quota tests. `git diff --check` and the
report's trailing-whitespace check also passed. After the restore/prefix fixes,
the focused backup and cluster-DR lane was rerun: 244 tests passed across
`tests/test_backup_restore.py` and
`tests/test_cluster_disaster_recovery.py`.

## Explicit remaining boundaries and cleanup state

At the evidence cutoff:

- All five supported production component drill paths completed successfully.
  The GitLab result proves the Toolbox artifact rather than a running GitLab
  service; the other results prove isolated PostgreSQL, Vault, one selected
  SeaweedFS PVC snapshot, and MongoDB recovery paths.
- No full Velero replay into a separately provisioned replacement cluster was
  executed. Such a drill must use a different Kubernetes context; the restore
  script rejects the recorded source context.
- No live profile migration was executed; only all 20 ordered plan paths and
  the fail-closed implementation contracts were tested.
- The isolated GitLab drill namespace was cleaned successfully. After the
  complete production bundle and GitLab archive drill, the disposable
  production GitLab namespace was explicitly removed to release test-account
  volume capacity; its remote archive and backup objects were retained. The
  subsequent production post-restore health snapshot was healthy.
- Whole-cluster/provider cleanup was not part of the acceptance evidence in
  this report. Before declaring the campaign closed, recheck the live provider
  and DNS state, remove only exact campaign-labeled resources, remove the
  disposable external DR endpoint after retaining any required evidence, and
  verify unrelated projects and DNS records remain unchanged.
- This report contains no credential values. Provider and DR credentials used
  for a disposable campaign should be rotated or invalidated after teardown.

The durable evidence remains under the gitignored
`.campaign-runtime/load5-260720-r5/results/` tree until operator cleanup. The
repository documentation and tests describe how to repeat every check without
depending on these ephemeral local paths.
