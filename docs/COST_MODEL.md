# Medium-optimized Hetzner cost model

This is the repository's audited cost baseline for the default
`medium-optimized` profile. Prices were read from Hetzner Cloud's authenticated
`/v1/pricing` API on 2026-07-22. They are net EUR values for the queried account,
whose API response reported 0% VAT. Re-query before purchasing because provider
prices, tax, and included traffic can change.

## Infrastructure

| Resource | Quantity | Hourly each | Monthly each | Monthly total |
|---|---:|---:|---:|---:|
| `cx33` control-plane and worker servers in `hel1` | 7 | €0.0136 | €8.49 | €59.43 |
| `cx23` bastion in `hel1` | 1 | €0.0088 | €5.49 | €5.49 |
| `lb11` in `hel1` | 1 | €0.0120 | €7.49 | €7.49 |
| Bastion Primary IPv4 | 1 | €0.0008 | €0.50 | €0.50 |
| **Infrastructure subtotal** | | **€0.1168** | | **€72.91** |

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
€72.910 infrastructure + €42.900 volumes = €115.810/month net
```

That is **€115.81/month net** rounded to cents. Hetzner's API supplies explicit
hourly rates for servers, the load balancer, and Primary IPv4, giving a direct
infrastructure rate of **€0.1168/hour**. It supplies volumes as a monthly
GiB price, not an hourly tariff. Dividing the complete monthly total by 730
hours gives **€0.15864/hour** as a planning equivalent only; it is not a
provider-quoted hourly price.

The API response listed 20 TiB included traffic and €1/TB excess traffic for
the relevant server and load-balancer price entries. This baseline excludes
traffic overages, external DR S3 or MinIO capacity, snapshots, Hetzner server
backups, domain registration, support, and any VAT applicable to another
customer. Private networks, firewalls, and placement groups have no separate
line item in this calculation.
