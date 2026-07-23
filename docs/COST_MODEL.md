# Medium-optimized Hetzner cost model

This is the repository's audited cost baseline for the default, currently
placeable `medium-optimized` profile. Prices and availability were read from
Hetzner Cloud's authenticated APIs on 2026-07-23. They are net EUR values for
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

## Persistent volume capacity

`scripts/profile-storage-capacity.py` applies Hetzner's 10 GiB minimum to every
claim. The default profile has 41 operational PVCs in 22 claim groups, plus
one staging PVC. The operational portion is 730 GiB and the total is 750 GiB
across 42 PVCs:

| Claim group | Billable GiB |
|---|---:|
| SeaweedFS masters, volumes, persistent indexes, and filer | 190 |
| Vault data and audit | 60 |
| PostgreSQL data and pgBackRest repo | 100 |
| MongoDB data | 60 |
| Elasticsearch masters and two data replicas | 110 |
| VictoriaMetrics, Alertmanager, Grafana, and PMM | 80 |
| GitLab Gitaly | 30 |
| Dragonfly | 10 |
| Coroot, ClickHouse, and Keepers | 60 |
| Tempo | 10 |
| Postal MariaDB | 20 |
| **Operational data claims** | **730** |
| GitLab backup staging | **20** |
| **Total billable volumes (42 PVCs)** | **750** |

The SeaweedFS index line is intentional. `medium-optimized` has three volume
replicas and keeps `storage.index_persistent: true`. Although each index asks
for 2 GiB, each separate Hetzner volume bills at the 10 GiB minimum, for 30 GiB
total. The disposable `--minimum-storage` test override colocates indexes and
is not the production cost baseline.

At €0.0572/GiB-month, operational data costs €41.756/month and GitLab staging
costs €1.144/month. Total volume cost is €42.900/month.

## Total

The full-month default is:

```text
€275.910 infrastructure + €42.900 volumes = €318.810/month net
```

That is **€318.81/month net** rounded to cents. Hetzner's API supplies explicit
hourly rates for servers, the load balancer, and Primary IPv4, giving a direct
uncapped infrastructure rate of **€0.4423/hour**. It supplies volumes as a
monthly GiB price, not an hourly tariff. Dividing the complete monthly-capped
total by 730 hours gives **€0.43673/hour** as a planning equivalent only; it is
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
| 750 GiB durable CSI volumes | | €0.0572/GiB | **€42.90** |
| **Total** | | | **€145.81** |

The Kubernetes nodes provide 44 vCPU, 88 GiB RAM, and 880 GiB aggregate
node-local SSD. The worker pool alone is 32 vCPU, 64 GiB RAM, and 640 GiB
local SSD—double the worker capacity of the earlier four-`cx33` mapping for
€30/month more. Including the bastion, the account receives 46 vCPU, 92 GiB
RAM, and 920 GiB local SSD. The direct infrastructure rate is €0.1648/hour;
the complete monthly-capped planning equivalent is €0.19974/hour.

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
