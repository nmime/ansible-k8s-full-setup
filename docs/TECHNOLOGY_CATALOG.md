# Technology Catalog and Selection Matrix

This is the exhaustive catalog for the canonical
`playbooks/deploy_platform.yml` workflow. The YAML selector is the source of
truth: a named profile is only a starting point, and any optional component
can be enabled later through `platform.sh` without rebuilding the cluster.

## Always-managed foundations

These foundations are not optional component switches because the platform
architecture depends on them.

| Area | Technologies and managed scope | Targeted reconcile |
|---|---|---|
| Cloud | Hetzner Cloud network, subnets, firewalls, placement groups, SSH key, bastion, control-plane/worker servers, load balancer, volumes, and selected DNS records | `deploy infra`, `deploy dns` |
| Host OS and security | Ubuntu 24.04, SSH key-only access, UFW, fail2ban, auditd, unattended upgrades, kernel/sysctl tuning, and node-exporter | `deploy network` |
| Private access | Headscale, embedded DERP/STUN, Caddy TLS termination behind an HAProxy SNI edge, private routes, and bastion NAT | `deploy network` |
| Kubernetes | Kubespray, Kubernetes, containerd, three topology shapes, encrypted secrets at rest, and schedulable-control-plane policy by profile | `deploy cluster` |
| Networking | Cilium CNI, transparent pod-network encryption, Hubble Relay/UI, network policies, and Cilium Gateway API | `deploy cluster`, `deploy network` |
| Traffic and TLS | Gateway API CRDs, public/admin Gateways, controller-owned NodePort discovery, Hetzner LB port/health convergence, minimal-tier bastion ingress, cert-manager, Hetzner DNS webhook, ClusterIssuers, Hetzner CCM/CSI, and MetalLB | `deploy cluster`, `deploy tls` |
| Secret bootstrap | Strong generated credentials persisted only in Ansible-Vault-encrypted local state | every deployment (`always` tag) |

`deploy tls` currently reconciles the cluster add-on bundle because
cert-manager, issuers, Gateway API, and their policies are coupled in that
role. `deploy dns` prints the declared-record preview; the infrastructure
reconcile manages only those platform records and does not authorize deletion
of unrelated records.

## Selectable technologies

Legend: **on** is enabled by the named profile; **off** can be enabled later.
The `medium-optimized` profile intentionally has the same technology set as
`medium`, but uses the `small` resource envelope plus explicit compact
overrides for heavy services.
The `production` profile also uses the conservative request envelope, while
explicitly retaining HA replicas for Vault, databases, storage, GitLab,
Argo CD, metrics, autoscaling, Temporal, alerting, tracing, and error tracking.
Its three control planes are dedicated and its three 16 GiB workers are sized
with failover headroom rather than steady-state-only fit.
Production explicitly pairs two VictoriaMetrics `vmstorage` replicas with
replication factor `2`, even though its pod request envelope is `small`.
`medium-optimized` intentionally keeps one `vmstorage` replica and replication
factor `1`; resource sizing therefore cannot silently change metrics durability.

Lifecycle component names: `object-storage`, `secrets`, `eso`, `databases`,
`postgresql`, `mongodb`, `elasticsearch`, `dragonfly`, `gitlab`,
`gitlab-runner`, `gitops`, `observability`, `pmm`, `coroot`, `tracing`, `autoscaling`,
`temporal`, `postal`, `backup`, `disaster-recovery`, `glitchtip`, `apm`,
`blackbox`, `daytona`, and `hipaa`.

| Component | YAML selector | Main technologies | Required selections | minimal | small | medium | medium-optimized | production |
|---|---|---|---|---:|---:|---:|---:|---:|
| Object storage | `storage.enabled` | SeaweedFS S3/filer/master/volume | none | on | on | on | on | on |
| Secrets | `secrets.enabled` | Vault Raft with internal TLS | none | on | on | on | on | on |
| ESO | `secrets.eso.enabled` | External Secrets Operator, Vault `ClusterSecretStore` | Secrets | off | off | on | on | on |
| Databases parent | `databases.enabled` | Percona operator bundle | at least one engine | on | on | on | on | on |
| PostgreSQL | `databases.postgresql.enabled` | Percona PostgreSQL, PgBouncer, pgBackRest; optional PMM client when PMM is selected | Databases | on | on | on | on | on |
| MongoDB | `databases.mongodb.enabled` | Percona Server for MongoDB and PBM; optional PMM client when PMM is selected | Databases | off | off | on | on | on |
| Elasticsearch | `elasticsearch.enabled` | Elasticsearch Basic, Kibana, TLS, ILM | none | off | off | on | on | on |
| Dragonfly | `dragonfly.enabled` | Dragonfly operator and Redis-compatible cache | none | off | on | on | on | on |
| GitLab | `gitlab.enabled` | GitLab CE, Gitaly, Registry, KAS, Toolbox | PostgreSQL, Dragonfly, object storage | off | on | on | on | on |
| GitLab Runner | `gitlab.runner.enabled` | GitLab Runner with S3 cache | GitLab | off | on | on | on | on |
| GitOps | `gitops.enabled` | Argo CD with scoped source/resource allowlists | none | on | on | on | on | on |
| Observability core | `observability.enabled` | VictoriaMetrics, Grafana, Alertmanager/VMAlert/VMRules, and Loki+Promtail or Elasticsearch+Filebeat/Fluentd | metrics, logging, Grafana subflags stay together | on | on | on | on | on |
| PMM | `observability.pmm.enabled` | Percona Monitoring and Management server plus database clients | Observability | off | off | on | on | on |
| Coroot | `coroot.enabled` | Coroot CE/operator, eBPF node agent, cluster agent, ClickHouse; VictoriaMetrics reused as external Prometheus | Observability | off | off | on | on | on |
| Tracing | `tracing.enabled` | Tempo and OpenTelemetry Collector | Observability, object storage | off | off | on | on | on |
| Autoscaling | `autoscaling.enabled` | KEDA | none | on | on | on | on | on |
| Temporal | `temporal.enabled` | Temporal server, UI, admin tools | PostgreSQL | off | off | on | on | on |
| Postal | `postal.enabled` | Postal mail server, schema initialize/update gate, and MariaDB; public SMTP 25/587 targets unprivileged container port 2525 | Dragonfly | off | off | on | on | on |
| Native backup automation | `backup.enabled` | GitLab, PostgreSQL, MongoDB, Vault, and SeaweedFS backup jobs plus application-aware restore drills | Object storage | off | off | on | on | on |
| External disaster recovery | `backup.disaster_recovery.enabled` | Velero/Kopia resource and mounted-PVC protection; complete encrypted etcd/PKI/config/cloud-state bundles; replacement-cluster restore | Native backup automation, object storage, and an independent external S3 endpoint | off | off | on | on | on |
| GlitchTip | `glitchtip.enabled` | GlitchTip error tracking | PostgreSQL, Dragonfly | off | off | on | on | on |
| APM | `apm.enabled` | Elastic APM Server and ILM bootstrap | Elasticsearch | off | off | on | on | on |
| Blackbox | `blackbox.enabled` | Prometheus Blackbox Exporter and VMProbe resources | Observability | off | off | on | on | on |
| Daytona | `applications.daytona.enabled` | Daytona workspace platform | none | off | off | off | off | off |
| HIPAA-oriented hardening | `compliance.hipaa.enabled` | Host audit rules, Vault TLS assertions, Cilium encryption assertion, and active log redaction | Secrets, observability | off | off | off | off | off |

Storage profiles with more than one SeaweedFS volume server select placement
`001`, migrate pre-existing single-copy volumes, fail if any volume remains
under-replicated, and restart the filer after master/Raft topology changes.
Minimal and small use `000` because they intentionally have only one volume
server. Normal profiles persist both SeaweedFS data and indexes. Disposable
`--minimum-storage` campaigns keep data durable but set
`storage.index_persistent: false` to colocate indexes on each durable data
claim instead of allocating a second CSI volume. The reconcile copies and
verifies every index inside a checkpointed write-quiesced maintenance window,
orphans the immutable StatefulSet, proves restart-safe pods plus a pre-existing
S3 sentinel read, and only then deletes exact obsolete standalone index claims.
Loki claims are retained independently of StatefulSet scale/delete and are
retired only by the checkpointed migration finalizer.

Alert transports are settings rather than removable workloads:
`alerting.telegram.enabled` requires `ALERT_TELEGRAM_BOT_TOKEN` and
`ALERT_TELEGRAM_CHAT_ID`; `alerting.email.enabled` requires Postal and an
`alerting.email.to` destination.

## Add, pause, return, and remove

```bash
cd platform-orchestrator
./platform.sh components
./platform.sh enable coroot       # enables required observability flags
./platform.sh validate            # offline dependency validation
./platform.sh deploy coroot       # targeted idempotent reconcile

./platform.sh disable coroot      # desired state only; workload is retained
./platform.sh enable coroot       # return later without data deletion
./platform.sh deploy coroot

# Native application backups and external whole-cluster DR are distinct.
./platform.sh enable disaster-recovery  # also enables backup + object storage
./platform.sh deploy disaster-recovery
```

`disable` never deletes a running workload. To reclaim capacity, first disable
the component and then call `remove` with exact confirmation. Data-bearing
components also require `--delete-data`. HIPAA-oriented hardening has no
generic automated rollback: disabling stops future reconciliation, while any
reversal must be reviewed control-by-control under change management.
When removing a parent such as observability, remove any already-disabled child
workloads such as Coroot/tracing first; disabled resources are intentionally
not inferred from live cluster state.

| Removal class | Components | Boundary |
|---|---|---|
| Data-bearing | object-storage, secrets, databases, PostgreSQL, MongoDB, Elasticsearch, Dragonfly, GitLab, GitOps, observability, PMM, Coroot, Temporal, Postal, GlitchTip, Daytona | exact confirmation plus `--delete-data` |
| Stateless/shared | ESO, GitLab Runner, tracing, KEDA, native backup jobs, external disaster-recovery controllers, APM, Blackbox | exact confirmation; remote trace/backup objects are retained |
| No generic rollback | HIPAA-oriented hardening | disable only; manual reviewed reversal |

Removal never deletes Hetzner infrastructure, platform DNS, external backup
copies, or remote object-storage buckets outside the listed Kubernetes scope.
Native-backup removal also clears PostgreSQL pgBackRest schedules and disables
MongoDB backup/PITR/tasks while retaining database data, repositories, PVCs,
and all previously created backup objects.

## Move between named profiles

Use `platform.sh migrate --target PROFILE plan`; never copy a different profile
over the active config and run `deploy all`. The migration engine covers every
distinct pair among the five named profiles, including `medium` ↔
`medium-optimized`, upgrades, and downgrades. The target named profile supplies
defaults only for selections the operator has not customized; component and
alert-channel choices that differ from the source profile's named defaults are
carried into the target and recorded in `selection-retention.tsv`.
Target reconciliation applies that merged selection, while technology deletion
is deferred to the separately confirmed, checkpointed `finalize` phase. A
removed technology can be selected again later and restored from its retained
external backup; external backup and Loki archive objects are never deleted by
profile finalization.

The transition expands to the larger node topology, resizes retained nodes one
at a time, grows both the provider disk and root filesystem, and requires full
platform health before touching the next node. It migrates VictoriaMetrics
between single and cluster mode when needed, then safely removes excess nodes
through Kubespray. Production taints control planes for general workloads but
allows critical PostgreSQL, MongoDB, and Elasticsearch stateful replicas to
tolerate them for one-worker failure capacity. Larger existing PVC
requests are retained as named-profile overrides because Kubernetes does not
support in-place shrink; obsolete component and old metrics-topology PVCs are
retired after the final backup gate. SeaweedFS, Vault Raft, and same-topology
VMCluster replicas are not reduced without their service-specific data
compaction or member-removal procedure; retained counts are explicit in the
migration plan and state.

The bastion is also retained without an in-place type change. Before mutation,
the migration reads its live provider type and writes it to every generated
source, transition, target, backup, and rollback config as well as durable
state. Resume repeats that read-only capture to heal older states and reconciles
the expansion spread placement group's exact project ownership label.

## Pinned platform versions

The authoritative values live in `defaults/main.yml` and role defaults. This
table is a review aid and must be updated with those values.

| Layer | Current pin |
|---|---|
| Kubernetes / Kubespray / Cilium / Gateway API | `v1.35.4` / `v2.31.0` / `v1.19.5` / `v1.6.0` |
| cert-manager / MetalLB / Hetzner CCM / Hetzner CSI | `v1.21.0` / `v0.16.1` / `v1.33.0` / `v2.22.0` |
| Headscale / Caddy | `0.28.0` / `2.11.4-alpine` |
| SeaweedFS chart | `4.25.1` |
| Vault chart / Vault / ESO chart | `0.34.0` / `2.0.3` / `2.7.0` |
| Percona PostgreSQL operator / PostgreSQL / PgBouncer / pgBackRest / PMM client | `3.0.0` / `18.4-1` / `1.25.2-1` / `2.58.0-2` / `3.7.1` |
| Percona MongoDB operator / MongoDB | `1.22.0` / `8.0.8-3` |
| Velero chart / Velero / AWS object-store plugin | `12.1.0` / `v1.18.1` / `v1.14.2` |
| GitLab / Runner charts | `10.1.2` / `0.88.3` |
| Argo CD chart / application | `9.5.14` / `v3.4.2` |
| VictoriaMetrics operator / Grafana / Loki / Promtail | `0.66.2` / `10.5.15` / `6.55.0` / `6.17.1` |
| PMM Server | `3.8.1` |
| Coroot operator chart / CE chart / application | `0.9.7` / `0.3.3` / `1.23.3` |
| Coroot node agent / cluster agent / ClickHouse | `1.34.2` / `1.7.1` / `25.11.2-ubi9-0` |
| Tempo chart+image / OTel chart+image | `1.6.1`+`2.6.1` / `0.102.1`+`0.112.0` |
| Elasticsearch / Kibana / APM | `9.4.3` / `9.4.3` / `9.4.3` |
| Dragonfly operator / image | `v1.6.1` / `v1.39.0` |
| KEDA / Temporal charts | `2.20.1` / `1.2.0` |
| Postal / GlitchTip chart+app / Blackbox chart / Daytona chart | `3.3.7` / `8.2.0`+`v6.1.4` / `11.15.1` / `0.0.23` |

Coroot uses its official operator architecture. The node agent needs privileged
Pod Security admission for eBPF and host inspection; that exception is scoped
to the `coroot` namespace. Coroot is configured with one ClickHouse
shard/replica and external VictoriaMetrics to avoid a duplicate Prometheus.
Global Secret reads are denied, and the UI uses the VPN/admin Gateway at
`coroot.<domain>` rather than the public Gateway.
The `medium-optimized` profile caps Coroot application and agent requests and
uses 10 GiB application plus 20 GiB ClickHouse storage.

## Separate explicit workflow

`playbooks/edge-cdn.yml` is deliberately outside `deploy_platform.yml` and the
selector. It provisions additional Ubuntu 22.04 Hetzner edge proxies, Nginx,
Let's Encrypt, node-exporter, and optional Gcore GeoDNS. It requires
`edge_cdn_confirm=true` and separate provider/origin inputs. It is not part of
any named cluster profile and is not removed by component lifecycle commands.

The unused `roles/seaweedfs-storage` directory is not called by the canonical
playbook; `roles/object-storage` is the active SeaweedFS implementation.
