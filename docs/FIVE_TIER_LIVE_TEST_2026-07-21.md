# Five-Profile Live Test Report — 2026-07-21

## Result and scope

All five named profiles were deployed concurrently as isolated Hetzner Cloud
clusters and exercised against delegated `n0xeid.xyz` DNS. The historical
five-profile acceptance evidence contains 23/23 Ready Kubernetes nodes, 141/141
protected PVCs, five passing profile-aware smoke runs, five healthy post-load
snapshots, five complete encrypted cluster bundles, five successful local
archive-verification runs, and 254,950 load operations with zero reported
errors. All five production component drill paths also passed: GitLab archive,
PostgreSQL, Vault, SeaweedFS, and MongoDB.

This report describes the disposable `load5-260720-r5` campaign. It does not
convert a successful backup verification into a claim that every application
has been restored. The exact restore and cleanup boundaries are recorded below.
Credential values, decrypted recovery material, provider identifiers, and
Secret contents are deliberately excluded. The later teardown and resumable
`minimal` to `production` migration changed the live topology; the acceptance
figures above are not a claim that all five clusters still exist. Current-state
statements below use the 2026-07-21T14:11:15Z evidence cutoff.

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

Before the migration began, the `minimal` cluster was safely resized one node
at a time to `cpx32` for both nodes after live testing showed that 2-vCPU/4-GiB
nodes could not keep the core stack plus one Velero node agent per node
schedulable. The profile floor is now 4 vCPU/8 GiB. This did not change its
two-node acceptance topology or technology set.

The authoritative selectable-component matrix, dependencies, and later
enable/disable/remove behavior remain in
[`TECHNOLOGY_CATALOG.md`](TECHNOLOGY_CATALOG.md).

## Historical five-profile acceptance evidence

The acceptance snapshots were collected after load with
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
APIServices, and 6/6 healthy provider-edge checks. This superseded the earlier
production snapshot for the five-profile acceptance phase. That disposable
production cluster was subsequently removed.

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

Each tier produced one complete age-encrypted acceptance bundle. The bundle
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

At the original evidence cutoff, the five archive/checksum/receipt triplets and
their Velero objects remained in external DR storage after provider teardown.
The later live `minimal` to `production` migration produced two additional
complete encrypted pre-switch triplets, timestamped `20260721T102455Z` and
`20260721T104447Z`. At that cutoff both were retained remotely; the first was
independently decrypted and verified, and the second was the durable
backup-stage checkpoint used by the active migration. These extra checkpoints
did not replace the original five-profile acceptance bundles. That remote-retention
state was later superseded by the incident described below: the old root-disk DR
payloads were lost, while the local encrypted checkpoint remained verifiable.

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
fail closed. The live `minimal` → `production` plan measured 110 GiB of
source claims, 1,310 GiB of target claims, a 1,220 GiB target delta, and 50 GiB
of migration scratch. It therefore requires 1,270 GiB of additional capacity
plus the configured 100 GiB safety margin.

This campaign proved the planning/state machine and quota boundary through the
automated source suite and one live `minimal` to `production` migration. Its
first execute attempt persisted resumable schema-v4 state and failed closed in
`preflight`: 1,440 GiB was already in use, the migration still required 1,270
GiB, and the 100 GiB margin projected a 2,810 GiB peak against the explicit
1,500 GiB account quota. No checkpoint or cluster mutation ran in that first
attempt.

After the four other disposable tier clusters were removed, account usage was
110 GiB and the same calculation projected 1,480 GiB including the safety
margin. `resume` passed preflight, completed the encrypted backup checkpoint,
and expanded the source from 1+1 to three control planes plus three workers.
Expansion completed with all six nodes Ready and healthy three-member etcd.
The migration then began its one-node-at-a-time resize. At the evidence cutoff,
five Kubernetes nodes had reached `cx43`, the remaining first control plane was
still `cpx32`, the retained bastion remained `cpx22`, and all six Kubernetes
nodes were Ready. The durable state still reported `status: in_progress` and
`last_completed_stage: expand`; the resize stage was not complete.

The live run also exercised fail-closed recovery paths. Backup initially found
that the rollback baseline used the wrong config/root, expansion found a live
bastion type that differed from the requested target, and provider placement
reconciliation restarted a powered-off node before its type change. The source
now captures the exact migration config/root, retains the authoritative live
bastion type, labels the spread placement group, and checks authoritative
provider power state both before and after placement. A Loki TSDB cache EOF
after the first worker drain stopped the health gate; the cache was quarantined
without deleting the active index, WAL, PVC, or remote objects, and the run did
not advance until Loki readiness and a fresh push/query succeeded. Resume now
gates every subsequent drain on live data-path health and records per-node
in-progress markers.

No later migration stage is claimed complete here. `resize`, Vault storage
migration, target reconciliation, VictoriaMetrics migration, target validation,
post-migration backup, and separately confirmed finalization still remained at
the cutoff. The automated suite validated all 20 ordered plans, but a live
all-to-all cutover matrix was not attempted.

## Post-cutoff destructive-provider incident

At `2026-07-21T15:50:54Z` through `15:52:33Z`, after the original evidence
cutoff, Hetzner recorded explicit API deletion actions for all seven expanded
`minimal` servers and the independent DR server. Nine captured CSI volumes
were explicitly deleted at `15:52:22Z`. This was not migration finalization:
durable migration state remains `in_progress`, its last completed stage is
`expand`, and no cleanup/finalize checkpoint exists. It was also not provider
TTL cleanup. Provider action history does not expose actor identity, and no
matching retained Codex execution, local shell history, cron job, launch job,
or surviving process identifies the caller. The action ordering is consistent
with project teardown and DR `down` being invoked in parallel from an
unrecorded shell, host, agent, console, or token holder; that is a forensic
inference, not proven attribution.

Two 10 GiB volumes survived because their detach actions did not complete until
after the immediate delete attempts. This exposed a teardown race: the old
implementation requested detach and deleted without waiting for authoritative
provider detachment. Teardown now waits for `.server == null`, fails with the
volume retained on timeout, and requires a second project-specific destructive
confirmation whenever any migration state remains active.

The latest local encrypted checkpoint
`load5-260720-minimal-cluster-20260721T104447Z` still verifies completely. It
contains repository/configuration state, encrypted platform secrets, Vault
initialization material, Kubernetes/Helm exports, Kubespray inventory, an etcd
snapshot, control-plane PKI, and cloud inventory. Its Velero metadata records
1,876 protected resources and 11 completed PodVolumeBackups. The bundle does
not embed the referenced Kopia packs or application-native object backups;
those payloads were stored in the deleted DR server's root filesystem. A full
PVC replay from this local archive is therefore impossible unless another copy
of the original DR object tree is recovered. Local raw Loki and one SeaweedFS
volume-server copy are retained only as partial forensic evidence, not a
supported logical restore.

The test DR helper now puts MinIO data on a dedicated campaign-labeled Hetzner
volume with provider delete protection. Normal `down` removes only disposable
compute, firewall, key, and DNS state; a later `up` safely reattaches the same
filesystem. Destructive data removal requires the exact separate
`PURGE <campaign> DR DATA` phrase. New backup manifests also record the source
cluster UID, and replacement restore compares that UID rather than relying on
Kubespray's frequently reused context name. Legacy bundles retain the stricter
context-name compatibility gate.

The durable DR lifecycle was then exercised live. A 10 GiB protected volume
was created, MinIO reached TLS health, and a 27-byte S3 sentinel was uploaded.
`down` removed the server and left the volume detached, labeled, and
delete-protected. A subsequent `up` attached that same volume to a newly
created server, reached TLS health, and returned the exact sentinel. The first
recreation attempt also exposed safe stale-host-key rejection when Hetzner
reassigned the previous IPv4 address; the helper now removes only that
provider-assigned address from its campaign-scoped trust file before accepting
the new server key. The successful rerun proves server-loss durability for the
new DR design; it does not recover objects already lost with the old root-disk
design.

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
- bounded GitLab Webservice memory headroom above the approximately 1.96 GiB
  live working set, hard per-component two-domain Rails placement, fail-closed
  Gitaly PDB verification, and enabled Runner Deployment convergence checks;
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
- persisted schema-v4 migration state, selection retention, volume-capacity
  planning, explicit SSH identity/trust and controller API-port retention,
  backup gates, rollback, resumability, and finalization ordering;
- exact project-label teardown and exact active-context membership checks,
  including bounded shutdown of the managed API tunnel before cleanup returns;
- migration-specific rollback-baseline config/root isolation, authoritative live
  bastion retention, exact placement-group ownership, and provider power-state
  checks before a node type change; and
- per-node resize recovery markers plus full data-path health checks before
  every subsequent drain, including Loki push/query and object-store sentinels.

## Source validation

The documentation/profile/backup/migration contract lane completed with 353
passing tests across `tests/test_backup_restore.py`,
`tests/test_platform_profiles.py`, and
`tests/test_cluster_disaster_recovery.py`. That run includes the 20 ordered
profile-pair plans and fail-closed volume-quota tests. `git diff --check` and the
report's trailing-whitespace check also passed. After the restore/prefix fixes,
the focused backup and cluster-DR lane was rerun: 244 tests passed across
`tests/test_backup_restore.py` and
`tests/test_cluster_disaster_recovery.py`. After teardown ownership,
credential-capture, SSH-state, and API-port hardening, the complete local gate
passed all ten checks and all 1,343 collected tests.

The later live migration findings produced additional focused migration tests
for rollback-baseline isolation, bastion retention, provider power-state and
capacity retries, interrupted-node recovery, and pre-drain health gating. The
1,343-test result predates those later commits and is not presented as a full
suite run of the final source. A new complete local gate remains required before
the campaign can be declared closed.

## Explicit remaining boundaries and cleanup state

At the evidence cutoff:

- All five supported production component drill paths completed successfully.
  The GitLab result proves the Toolbox artifact rather than a running GitLab
  service; the other results prove isolated PostgreSQL, Vault, one selected
  SeaweedFS PVC snapshot, and MongoDB recovery paths.
- No full Velero replay into a separately provisioned replacement cluster was
  executed. Such a drill must use a different Kubernetes context; the restore
  script rejects the recorded source context.
- All 20 ordered plan paths were validated in the source suite. Only the live
  `minimal` to `production` transition was attempted; it passed the resumed
  preflight, backup, and expansion checkpoints but had not completed resize or
  any later cutover/finalization stage.
- The isolated GitLab drill namespace was cleaned successfully. After the
  complete production bundle and GitLab archive drill, the disposable
  production GitLab namespace was explicitly removed to release test-account
  volume capacity; its remote archive and backup objects were retained. The
  subsequent production post-restore health snapshot was healthy.
- The `small`, `medium`, `medium-optimized`, and `production` clusters were
  removed in parallel and idempotent teardown reruns completed successfully.
  Exact project-labeled compute/network resources and their captured CSI
  volumes were absent afterward. Their encrypted local controller state,
  external backup objects, and DNS records were intentionally retained.
- The live provider still contained the expanded `minimal` migration project
  and the independent DR endpoint. Teardown intentionally preserves DNS, so
  the old A records for all five tier domains remained and must not be mistaken
  for live-cluster proof. The DR endpoint, bucket, and its DNS record must remain
  until required recovery evidence is retained independently.
- This report contains no credential values. The repository `.env` is
  gitignored and mode `0600`, while encrypted campaign-state directories are
  mode `0700`. A temporary raw Loki pre-repair copy under the gitignored runtime
  tree is plaintext workload data and must be permission-restricted, encrypted,
  or removed after recovery verification. Object-storage credentials exposed
  during diagnostics must be rotated; disposable provider and DR credentials
  should also be rotated or invalidated after final teardown.

The acceptance results remain under the gitignored
`.campaign-runtime/load5-260720-r5/results/` tree, while migration checkpoints
remain under the adjacent private migration state until operator cleanup. The
repository documentation and tests describe how to repeat every check without
depending on these ephemeral local paths.

## Post-cutoff source status — 2026-07-23

The final recovery implementation passed the repository-wide
`scripts/validate-local.sh` gate after the replacement exercise and its
follow-up hardening changes. All 10 mandatory checks passed, including the
complete pytest suite. Focused recovery testing also exercises interruption
after PostgreSQL deletion and while an exact completed cluster is still
becoming Ready. Publication and local/remote `main` parity are verified as a
separate delivery gate rather than recorded through a self-referential commit
hash in this file.

## Medium-optimized cost audit — 2026-07-23

The authenticated Hetzner pricing API returned `cx33` at €8.49/month,
`cx23` at €5.49/month, `lb11` at €7.49/month, the bastion IPv4 at €0.50/month,
and volumes at €0.0572/GiB-month, net with 0% VAT for the queried account.
The campaign commit resolved the profile to seven `cx33` nodes, one `cx23`
bastion, one `lb11`, one paid IPv4, 730 GiB of operational persistent claims,
and 20 GiB of GitLab backup staging. The exact historical full-month total is
therefore
€115.810 net (€115.81 rounded). External DR storage, snapshots, excess traffic,
domain registration, and non-zero customer VAT are excluded. The reconciled
claim inventory and arithmetic are maintained in [the cost model](COST_MODEL.md).
At the same query time, `hel1` marked the relevant `cx23`, `cx33`, and `cx43`
types unavailable for new placement, so the price is not a current capacity
guarantee.

After this campaign, the named profiles moved their purchase defaults to the
predictably available CPX generation. The €115.81 figure above remains the
historical all-`cx33` campaign cost, not the current CX purchase mapping. The
current mapping retains three `cx33` control planes, promotes four workers to
`cx43`, and assigns only application-replicated claims to local SSD. Its
current estimate is €118.93/month, while doubling the worker pool's CPU, RAM,
and local SSD. Current mappings and live refresh commands are documented in
[Hetzner capacity tariffs](HETZNER_CAPACITY_TARIFFS.md).

## Completed replacement-recovery campaign — 2026-07-23

The encrypted source recovery point
`load5-restore-prod-cluster-20260722T180312Z` was restored into a separately
provisioned production replacement with three control planes and three workers.
The source and target `kube-system` UIDs were different. Velero Restore
`load5-restore-prod-cluster-20260722t180312z-restore-20260723041724` completed
2,742/2,742 resource items with zero errors. All 41 PodVolumeRestores completed.
Its 1,005 warnings were explicitly allowed only after review as expected
existing-resource collisions on the pre-bootstrapped replacement.

The schema-v2 native replay then completed in dependency order for SeaweedFS,
Vault, PostgreSQL, MongoDB, GitLab Rails secrets, and GitLab Toolbox data.
Exact artifact locators were checked before mutation. Vault members were
unsealed and verified, PostgreSQL proved the recorded set through the live
pgBackRest catalog, MongoDB proved the exact PBM metadata object, and GitLab
restored without transporting its Rails secret through pod logs. Full
reconciliation completed with Ansible recap `ok=675 changed=70 unreachable=0
failed=0 skipped=148 rescued=0 ignored=0`.

Post-reconciliation health passed with 6/6 nodes, 6/6 Cilium agents, 10/10
cert-manager pods, 55/55 aggregated API services, 10/10 Argo CD pods, 6/6
PostgreSQL pods, and 4/4 MongoDB pods. There were zero unhealthy workload
objects, PVCs, certificates, HTTPRoutes, privileged application containers,
invalid CiliumNetworkPolicies, or failed Helm releases. Authenticated live
smoke passed every configured route plus S3, PostgreSQL transaction rollback,
Vault KV, KEDA metrics, Elasticsearch, Grafana, and Argo CD. The GitLab Runner
Deployment converged one ready replica under production concurrency four; this
proves runner capacity configuration, not execution of a real GitLab pipeline.

The `replacement-post-recovery` load run passed 159,000 operations with zero
errors and zero allowed restart delta: HTTP 20,000 in 289 seconds, S3 4,000 in
48 seconds, PostgreSQL 20,000 in 84 seconds, Vault 15,000 in 462 seconds, and
Dragonfly 100,000 in 39 seconds. A final health run passed afterward.

Fresh encrypted recovery point
`load5-restore-prod-cluster-20260723T060931Z` then completed all six native
backups. Velero completed 3,421/3,421 items and 41/41 PodVolumeBackups with zero
errors and zero warnings. Its schema-v2 receipt binds target UID
`7fc558bf-db0b-4b9e-883f-38c1dc7b2a87`, records receipt-last remote
publication, and proves the downloaded archive SHA-256. This exact receipt
passed the teardown gate before the first destructive provider mutation.

The backup exercise also exposed a restored stale PostgreSQL backup Lease that
blocked the first new pgBackRest backup. The disposable Lease was removed after
proving its holder Job absent, and replacement Restore now excludes all
`leases.coordination.k8s.io` alongside cert-manager `CertificateRequest`
objects because both are cluster-local transient state.

Receipt-gated cleanup removed all seven replacement servers, its load balancer,
41 CSI volumes, firewalls, placement group, network, and API tunnel. The
remaining orphaned `minimal` test network and placement group were also removed,
and all disposable `load5-*` DNS RRsets were deleted. Final provider queries
returned zero matching servers, load balancers, networks, firewalls, placement
groups, and DNS RRsets. The only retained campaign resource is the detached,
delete-protected 100 GiB `load5-260720-dr-data` recovery volume. Its DR server
and DNS record are down; bringing it back requires the separately retained
credentials.

The VictoriaMetrics exact-value/timestamp and rollback-delta contract remains
source-tested. The earlier live minimal-to-production profile migration stopped
at its recorded capacity boundary, so this report does not misrepresent that
particular live migration as finalized.
