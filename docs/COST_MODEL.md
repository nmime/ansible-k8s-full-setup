# Medium-optimized Hetzner cost model

This is the repository's audited cost baseline for the default, currently
placeable `medium-optimized` profile. Prices and availability were read from
Hetzner Cloud's authenticated APIs on 2026-07-24. They are net EUR values for
the queried account, whose API response reported 0% VAT. Re-query before
purchasing because provider prices, tax, and included traffic can change:

```bash
./scripts/hetzner-capacity-report.sh --location hel1
```

## Infrastructure

| Resource | Quantity | Hourly each | Monthly each | Monthly total |
|---|---:|---:|---:|---:|
| `cpx32` control-plane and worker servers in `hel1` | 7 | €0.0569 | €35.49 | €248.43 |
| `cpx22` bastion in `hel1` | 1 | €0.0312 | €19.49 | €19.49 |
| `lb11` in `hel1` | 1 | €0.0120 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.0008 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | **€0.4423** | | **€275.91** |

Control-plane and worker servers are created without public IPv4 or IPv6;
only the bastion receives the separately billed Primary IPv4. The load
balancer's public address is part of the load-balancer resource.

## Hybrid persistent capacity

The base profile has 32 operational PVCs in 17 claim groups and no GitLab
staging PVC. It has a conservative 520 GiB active-claim envelope and assigns only
application-replicated data to server-local SSD:

| Claim group | Storage class | Reserved GiB |
|---|---|---:|
| SeaweedFS masters, volume servers, and persistent indexes | `platform-local` | 180 |
| Vault Raft data | `platform-local` | 30 |
| Elasticsearch masters and two data replicas | `platform-local` | 110 |
| **Active replication-qualified local claims** | | **320** |
| SeaweedFS filer | `hcloud-volumes` | 10 |
| Vault audit claims | `hcloud-volumes` | 30 |
| VictoriaMetrics, Alertmanager, Grafana, and PMM | `hcloud-volumes` | 80 |
| Dragonfly | `hcloud-volumes` | 10 |
| Coroot, ClickHouse, and Keepers | `hcloud-volumes` | 60 |
| Tempo | `hcloud-volumes` | 10 |
| **Provider-billable CSI capacity** | | **200** |
| **Total conservative active-claim envelope** | | **520** |

`scripts/profile-storage-capacity.py` applies Hetzner's 10 GiB minimum to CSI
claims and uses the same minimum as a conservative reservation for small local
claims. Actual Kubernetes requests total 462 GiB; the larger 520 GiB envelope
prevents the cost and capacity plan from depending on sub-10-GiB packing.

The playbook creates 23 static local PV slots totaling 470 GiB. Kubernetes uses
`WaitForFirstConsumer` and explicit node affinity to account capacity during
scheduling; every PV is retained. A deployment-time DaemonSet rejects nodes
with less than 40 GiB free at the configured path, while a separate node gate
requires at least 70 GiB of root-disk capacity on each control plane and
140 GiB on each worker. The base setup actively uses 17 slots/320 GiB; the
remaining 150 GiB is the exact expandable capacity for the profile's
three-replica PostgreSQL (90 GiB) and MongoDB (60 GiB) opt-ins. SeaweedFS,
Vault, PostgreSQL, MongoDB, and Elasticsearch use
required or chart-provided hostname anti-affinity and application replication.
Local volumes still cannot move with a failed node; application quorum and
external native/Velero backups are therefore recovery requirements, not
optional optimizations. Singleton, audit, and staging claims stay on Hetzner
CSI when their owning component is selected.

The static PV sizes are scheduler reservations, not filesystem quotas: all
slots on a node share its root filesystem. Workloads must keep their own data
within the requested size. `NodeDiskUsageHigh` at 85%, kubelet `DiskPressure`,
and the retained 40 GiB deployment gate protect the remaining operating-system
and container-runtime headroom.

At €0.0572/GiB-month, 200 GiB of provider volumes costs €11.440/month. The
470 GiB expandable static pool is already included in server prices. Enabling
PostgreSQL, MongoDB, and GitLab adds the component pods and claims; the prior
all-selected storage ceiling is not charged in the base profile.

## Total

The full-month default is:

```text
€275.910 infrastructure + €11.440 volumes = €287.350/month net
```

That is **€287.35/month net** rounded to cents. Hetzner's API supplies explicit
hourly rates for servers, the load balancer, and Primary IPv4, giving a direct
uncapped infrastructure rate of **€0.4423/hour**. It supplies volumes as a
monthly GiB price, not an hourly tariff. Dividing the complete monthly-capped
total by 730 hours gives **€0.39363/hour** as a planning equivalent only; it is
not a provider-quoted hourly price.

The API response listed 20 TiB included traffic and €1/TB excess traffic for
the relevant server and load-balancer price entries. This baseline excludes
traffic overages, external DR S3 or MinIO capacity, snapshots, Hetzner server
backups, domain registration, support, and any VAT applicable to another
customer. Private networks, firewalls, and placement groups have no separate
line item in this calculation.

## Intermittent cost-optimized reference

The optimized CX mapping uses three `cx33` schedulable control planes, four
`cx43` workers, and one `cx23` bastion:

| Resource | Quantity | Monthly each | Monthly total |
|---|---:|---:|---:|
| `cx33` control planes | 3 | €8.49 | €25.47 |
| `cx43` workers | 4 | €15.99 | €63.96 |
| `cx23` bastion | 1 | €5.49 | €5.49 |
| `lb11` | 1 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | | **€102.91** |
| 200 GiB durable CSI volumes | | €0.0572/GiB | **€11.44** |
| 320 GiB active claims in a 470 GiB server-SSD pool | | included | **€0.00** |
| **Total** | | | **€114.35** |

The Kubernetes nodes provide 44 vCPU, 88 GiB RAM, and 880 GiB aggregate
node-local SSD. The worker pool alone is 32 vCPU, 64 GiB RAM, and 640 GiB
local SSD—double the worker capacity of the earlier four-`cx33` mapping for
€30/month more. Including the bastion, the account receives 46 vCPU, 92 GiB
RAM, and 920 GiB local SSD. The direct infrastructure rate is €0.1648/hour;
the complete monthly-capped planning equivalent is €0.15664/hour.

This mapping is retained as an opportunistic purchase option and
migration/rollback target. CX capacity appears intermittently; at audit time,
`hel1` marked the required types temporarily unavailable for new placement.

The default named profile uses the predictably available CPX mapping. Existing CX
clusters are not resized by an ordinary reconcile: the infrastructure role
fails closed on type drift, and `scripts/migrate-profile.sh` owns backed-up,
one-node-at-a-time type migration.

The full CX, CAX, CPX, and CCX catalog, availability state, mappings, and totals
for all five profiles are maintained in
[Hetzner capacity tariffs](HETZNER_CAPACITY_TARIFFS.md).
