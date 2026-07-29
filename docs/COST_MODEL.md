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
| `cpx32` control-plane and worker servers in `hel1` | 6 | €0.0569 | €35.49 | €212.94 |
| `cpx22` bastion in `hel1` | 1 | €0.0312 | €19.49 | €19.49 |
| `lb11` in `hel1` | 1 | €0.0120 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.0008 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | **€0.3854** | | **€240.42** |

Control-plane and worker servers are created without public IPv4 or IPv6;
only the bastion receives the separately billed Primary IPv4. The load
balancer's public address is part of the load-balancer resource.

## Hybrid persistent capacity

The base profile has 31 operational PVCs in 17 claim groups plus one GitLab
staging PVC. It has a conservative 540 GiB active-claim envelope and assigns only
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
| GitLab backup staging | `hcloud-volumes` | 20 |
| **Provider-billable CSI capacity** | | **240** |
| **Total conservative active-claim envelope** | | **540** |

`scripts/profile-storage-capacity.py` applies Hetzner's 10 GiB minimum to CSI
claims and uses the same minimum as a conservative reservation for small local
claims. Operational Kubernetes requests total 462 GiB and conservatively
reserve 520 GiB; adding the 20 GiB GitLab staging claim produces 482 GiB
requested and the larger 540 GiB envelope. This
prevents the cost and capacity plan from depending on sub-10-GiB packing.

The playbook creates 24 target-generation static local PV slots totaling
450 GiB. Kubernetes uses
`WaitForFirstConsumer` and explicit node affinity to account capacity during
scheduling; every PV is retained. A deployment-time DaemonSet rejects nodes
with less than 40 GiB free at the configured path, while a separate node gate
requires at least 70 GiB of root-disk capacity on each control plane and
140 GiB on each worker. The base setup actively uses 15 slots/300 GiB. The
remaining capacity supports the three-replica MongoDB opt-in, two worker-local
Nx cache claims, and bounded operational headroom. SeaweedFS,
Vault, PostgreSQL, and MongoDB use
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

At €0.0572/GiB-month, 240 GiB of provider volumes costs €13.728/month, rounded
to €13.73. The 450 GiB expandable static pool is already included in server
prices. Enabling MongoDB consumes only the remaining replicated local-pool
capacity and therefore does not increase this provider-volume baseline.

## Total

The full-month default is:

```text
€240.420 infrastructure + €13.728 volumes = €254.148/month net
```

That is **€254.15/month net** rounded to cents. Hetzner's API supplies explicit
hourly rates for servers, the load balancer, and Primary IPv4, giving a direct
uncapped infrastructure rate of **€0.3854/hour**. It supplies volumes as a
monthly GiB price, not an hourly tariff. Dividing the complete monthly-capped
total by 730 hours gives **€0.34815/hour** as a planning equivalent only; it is
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
| 240 GiB durable CSI volumes | | €0.0572/GiB | **€13.73** |
| 300 GiB active claims in a 360 GiB server-SSD pool | | included | **€0.00** |
| **Total** | | | **€100.65** |

The Kubernetes nodes provide 36 vCPU, 72 GiB RAM, and 720 GiB aggregate
node-local SSD. The worker pool alone is 24 vCPU, 48 GiB RAM, and 480 GiB
local SSD. Including the bastion, the account receives 38 vCPU, 76 GiB RAM,
and 760 GiB local SSD. The direct infrastructure rate is €0.1392/hour; the
complete monthly-capped planning equivalent is €0.13787/hour.

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
