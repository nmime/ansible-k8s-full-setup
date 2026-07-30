# Medium-optimized Hetzner cost model

This is the repository's audited cost baseline for the default, currently
placeable `medium-optimized` profile and the live CX deployment. Prices and availability were read from
Hetzner Cloud's authenticated APIs on 2026-07-30. They are net EUR values for
the queried account, whose API response reported 0% VAT. Re-query before
purchasing because provider prices, tax, and included traffic can change:

```bash
./scripts/hetzner-capacity-report.sh --location hel1
```

## CPX platform base

| Resource | Quantity | Hourly each | Monthly each | Monthly total |
|---|---:|---:|---:|---:|
| `cpx32` control-plane and worker servers in `hel1` | 6 | €0.0569 | €35.49 | €212.94 |
| `cpx22` bastion in `hel1` | 1 | €0.0312 | €19.49 | €19.49 |
| `lb11` in `hel1` | 1 | €0.0120 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.0008 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | **€0.3854** | | **€240.42** |

Control-plane and worker servers are created without public IPv4 or IPv6;
only the bastion receives the separately billed Primary IPv4. The load
balancer's public address is part of the load-balancer resource.

## Hybrid persistent capacity

The base profile has 31 operational PVCs in 17 claim groups. It has a
conservative 520 GiB active-claim envelope and assigns only
application-replicated data to server-local SSD:

| Claim group | Storage class | Reserved GiB |
|---|---|---:|
| SeaweedFS masters, volume servers, and persistent indexes | `platform-local` | 180 |
| Vault Raft data | `platform-local` | 30 |
| PostgreSQL instance data | `platform-local` | 90 |
| **Active replication-qualified local claims** | | **300** |
| SeaweedFS filer | `hcloud-volumes` | 10 |
| Vault audit claims | `hcloud-volumes` | 30 |
| pgBackRest repository | `hcloud-volumes` | 10 |
| VictoriaMetrics, Alertmanager, Grafana, and Loki | `hcloud-volumes` | 70 |
| GitLab Gitaly | `hcloud-volumes` | 30 |
| Dragonfly | `hcloud-volumes` | 10 |
| Coroot, ClickHouse, and Keepers | `hcloud-volumes` | 60 |
| **Provider-billable CSI capacity** | | **220** |
| **Total conservative active-claim envelope** | | **520** |

`scripts/profile-storage-capacity.py` applies Hetzner's 10 GiB minimum to CSI
claims and uses the same minimum as a conservative reservation for small local
claims. Operational Kubernetes requests total 462 GiB and conservatively
reserve 520 GiB. This
prevents the cost and capacity plan from depending on sub-10-GiB packing.

The playbook creates 24 target-generation static local PV slots totaling
450 GiB. Kubernetes uses
`WaitForFirstConsumer` and explicit node affinity to account capacity during
scheduling; every PV is retained. A deployment-time DaemonSet rejects nodes
with less than 40 GiB free at the configured path, while a separate node gate
requires at least 70 GiB of root-disk capacity on each control plane and
140 GiB on each worker. The base setup actively uses 15 slots/300 GiB; the
three reserved 20 GiB slots supply the exact data capacity for the
three-replica MongoDB opt-in. SeaweedFS,
Vault, PostgreSQL, and MongoDB use
required or chart-provided hostname anti-affinity and application replication.
Local volumes still cannot move with a failed node; application quorum and
external native/Velero backups are therefore recovery requirements, not
optional optimizations. Singleton and audit claims stay on Hetzner CSI when
their owning component is selected. GitLab backup staging is transient
node-local scratch uploaded immediately to object storage.

The static PV sizes are scheduler reservations, not filesystem quotas: all
slots on a node share its root filesystem. Workloads must keep their own data
within the requested size. `NodeDiskUsageHigh` at 85%, kubelet `DiskPressure`,
and the retained 40 GiB deployment gate protect the remaining operating-system
and container-runtime headroom.

At €0.0572/GiB-month, 220 GiB of provider volumes costs €12.584/month, rounded
to €12.58. The 450 GiB expandable static pool is already included in server
prices. GitLab uses up to 30 GiB of transient node-local backup scratch and
uploads completed archives to object storage; that scratch is not durable
state. Enabling MongoDB consumes only the remaining replicated local-pool
capacity and therefore does not increase this provider-volume baseline.

## CPX platform-base total

The six-node workload platform base, before the isolated CI worker, is:

```text
€240.420 infrastructure + €12.584 volumes = €253.004/month net
```

That is **€253.00/month net** rounded to cents. Adding the isolated `cpx32`
Docker worker and the isolated `cpx42` general/image-build worker makes the
complete currently placeable named profile **€357.98/month net**. Hetzner's
API supplies explicit
hourly rates for servers, the load balancer, and Primary IPv4, giving a direct
uncapped infrastructure rate of **€0.3854/hour**. It supplies volumes as a
monthly GiB price, not an hourly tariff. Dividing the complete monthly-capped
total by 730 hours gives **€0.49039/hour** as a planning equivalent only; it is
not a provider-quoted hourly price.

The API response listed 20 TiB included traffic and €1/TB excess traffic for
the relevant server and load-balancer price entries. This baseline excludes
traffic overages, external DR S3 or MinIO capacity, snapshots, Hetzner server
backups, domain registration, support, and any VAT applicable to another
customer. Private networks, firewalls, and placement groups have no separate
line item in this calculation.

## Intermittent cost-optimized reference

The optimized CX mapping uses three `cx33` schedulable control planes, three
`cx43` workers, and one `cx23` bastion:

| Resource | Quantity | Monthly each | Monthly total |
|---|---:|---:|---:|
| `cx33` control planes | 3 | €8.49 | €25.47 |
| `cx43` workers | 3 | €15.99 | €47.97 |
| `cx23` bastion | 1 | €5.49 | €5.49 |
| `lb11` | 1 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | | **€86.92** |
| 220 GiB durable CSI volumes | | €0.0572/GiB | **€12.58** |
| Up to 30 GiB transient GitLab backup scratch | | server-local SSD | **€0.00** |
| 300 GiB active claims in a 450 GiB server-SSD pool | | included | **€0.00** |
| **Total** | | | **€99.50** |

The production deployment uses two tainted CI workers. Worker 4 runs only the
protected Docker-in-Docker compatibility lane. Worker 5 runs one general job
and one protected rootless image build at most. Neither is an ingress target,
receives local PVs, or has a public IP. On 2026-07-30 all CX33/CX43 placements
were unavailable in every EU location, while CPX32 and CPX42 were placeable:

| Additional resource | Quantity | Monthly each | Monthly total |
|---|---:|---:|---:|
| Isolated `cpx32` Docker CI worker | 1 | €35.49 | **€35.49** |
| Isolated `cpx42` general/image CI worker | 1 | €69.49 | **€69.49** |
| **Infrastructure subtotal with CI** | | | **€191.90** |
| 220 GiB durable CSI volumes | | €0.0572/GiB | **€12.58** |
| **Live CX production total with CI** | | | **€204.48** |

The Kubernetes nodes then provide 48 vCPU, 96 GiB RAM, and 1,200 GiB aggregate
node-local SSD. Only the original three `cx43` workers contribute to the
replication-qualified local-PV pool. Including the bastion, the account
receives 50 vCPU, 100 GiB RAM, and 1,240 GiB local SSD. The direct
infrastructure rate is €0.3071/hour; the complete monthly-capped planning
equivalent is €0.28012/hour.

When CX capacity returns, migrate each CI worker separately through the
one-node-at-a-time migration gate. Keep at least 16 GiB RAM and 320 GB SSD for
the general/image lane; `cx53` is the first legacy shape matching both. Do not
resize the application workers or count either CI disk as durable storage.

The original 3+3 mapping is retained as an opportunistic purchase option and
migration/rollback target. CX capacity appears intermittently; at the latest
placement check, every EU location marked `cx33` and `cx43` unavailable for
new placement.

The default named profile uses the predictably available CPX mapping. Existing CX
clusters are not resized by an ordinary reconcile: the infrastructure role
fails closed on type drift, and `scripts/migrate-profile.sh` owns backed-up,
one-node-at-a-time type migration.

The full CX, CAX, CPX, and CCX catalog, availability state, mappings, and totals
for all five profiles are maintained in
[Hetzner capacity tariffs](HETZNER_CAPACITY_TARIFFS.md).
