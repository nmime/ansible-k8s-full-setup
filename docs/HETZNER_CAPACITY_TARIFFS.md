# Hetzner capacity tariffs

This document records the authenticated Hetzner Cloud catalog observed for
`hel1` on 2026-07-23. The repository does not treat this snapshot as a placement
guarantee. Refresh the catalog immediately before provisioning:

```bash
./scripts/hetzner-capacity-report.sh --location hel1
./scripts/hetzner-capacity-report.sh --location hel1 --format json
```

The wrapper loads `HCLOUD_TOKEN` through the repository's mode-`0600`,
gitignored `.env` loader. It does not print the token. The report queries the
server-type and pricing APIs, inventories every returned server type, applies
the exact named-profile topology and PVC estimator, and calculates current net
monthly totals.

## Capacity tariff policy

| Tariff | Hetzner family | Architecture | Deployment policy |
|---|---|---|---|
| Cost-optimized x86 | `CX` | x86 shared | Opt-in; availability is intermittent and must be queried immediately before creation |
| Economy ARM | `CAX` | ARM64 shared | Planning only until the complete selected image/runtime set is attested on ARM64 |
| Balanced | `CPX` | x86 shared | Default for all new named-profile deployments |
| Dedicated | `CCX` | x86 dedicated | Opt-in for sustained CPU and predictable performance |

Use `--capacity-family cx`, `cax`, `cpx`, or `ccx` with `run_tier.sh` or
`run_all.sh`. `run_all.sh` defaults to `cpx`. `cax` can produce a dry-run cost
and desired-state plan but is rejected before a live deployment because the
full platform has not passed its ARM64 production gate. Explicit
`--bastion-type`, `--cp-type`, and `--worker-type` remain available for reviewed
custom shapes and cannot be combined with a capacity-family selection.

## Complete server catalog

Net prices and availability below are for the queried account and `hel1`.
Unavailable generations remain visible because existing servers can continue
to run and migration/rollback tooling must understand their types.

| Type | Family | CPU | vCPU | RAM | Local SSD | Traffic | Available | Net/hour | Net/month |
|---|---|---|---:|---:|---:|---:|:---:|---:|---:|
| `cx23` | CX | shared x86 | 2 | 4 GiB | 40 GiB | 20 TiB | no | €0.0088 | €5.49 |
| `cx33` | CX | shared x86 | 4 | 8 GiB | 80 GiB | 20 TiB | no | €0.0136 | €8.49 |
| `cx43` | CX | shared x86 | 8 | 16 GiB | 160 GiB | 20 TiB | no | €0.0256 | €15.99 |
| `cx53` | CX | shared x86 | 16 | 32 GiB | 320 GiB | 20 TiB | no | €0.0473 | €29.49 |
| `cax11` | CAX | shared ARM64 | 2 | 4 GiB | 40 GiB | 20 TiB | no | €0.0096 | €5.99 |
| `cax21` | CAX | shared ARM64 | 4 | 8 GiB | 80 GiB | 20 TiB | no | €0.0168 | €10.49 |
| `cax31` | CAX | shared ARM64 | 8 | 16 GiB | 160 GiB | 20 TiB | no | €0.0336 | €20.99 |
| `cax41` | CAX | shared ARM64 | 16 | 32 GiB | 320 GiB | 20 TiB | no | €0.0657 | €40.99 |
| `cpx11` | CPX legacy | shared x86 | 2 | 2 GiB | 40 GiB | 20 TiB | no | €0.0088 | €5.49 |
| `cpx21` | CPX legacy | shared x86 | 3 | 4 GiB | 80 GiB | 20 TiB | no | €0.0152 | €9.49 |
| `cpx31` | CPX legacy | shared x86 | 4 | 8 GiB | 160 GiB | 20 TiB | no | €0.0280 | €17.49 |
| `cpx41` | CPX legacy | shared x86 | 8 | 16 GiB | 240 GiB | 20 TiB | no | €0.0521 | €32.49 |
| `cpx51` | CPX legacy | shared x86 | 16 | 32 GiB | 360 GiB | 20 TiB | no | €0.1138 | €70.99 |
| `cpx12` | CPX current | shared x86 | 1 | 2 GiB | 40 GiB | 20 TiB | yes | €0.0184 | €11.49 |
| `cpx22` | CPX current | shared x86 | 2 | 4 GiB | 80 GiB | 20 TiB | yes | €0.0312 | €19.49 |
| `cpx32` | CPX current | shared x86 | 4 | 8 GiB | 160 GiB | 20 TiB | yes | €0.0569 | €35.49 |
| `cpx42` | CPX current | shared x86 | 8 | 16 GiB | 320 GiB | 20 TiB | yes | €0.1114 | €69.49 |
| `cpx52` | CPX current | shared x86 | 12 | 24 GiB | 480 GiB | 20 TiB | yes | €0.1610 | €100.49 |
| `cpx62` | CPX current | shared x86 | 16 | 32 GiB | 640 GiB | 20 TiB | yes | €0.2083 | €129.99 |
| `ccx13` | CCX | dedicated x86 | 2 | 8 GiB | 80 GiB | 20 TiB | yes | €0.0689 | €42.99 |
| `ccx23` | CCX | dedicated x86 | 4 | 16 GiB | 160 GiB | 20 TiB | yes | €0.1378 | €85.99 |
| `ccx33` | CCX | dedicated x86 | 8 | 32 GiB | 240 GiB | 30 TiB | yes | €0.2219 | €138.49 |
| `ccx43` | CCX | dedicated x86 | 16 | 64 GiB | 360 GiB | 40 TiB | yes | €0.4423 | €275.99 |
| `ccx53` | CCX | dedicated x86 | 32 | 128 GiB | 600 GiB | 50 TiB | yes | €0.8550 | €533.49 |
| `ccx63` | CCX | dedicated x86 | 48 | 192 GiB | 960 GiB | 60 TiB | yes | €1.3678 | €853.49 |

The authenticated EU price entries reported €1.00/TB for traffic above the
included allowance.

## Balanced profile mappings

| Profile | Bastion / control plane / worker |
|---|---|
| `minimal` | `cpx22` / `cpx32` / `cpx32` |
| `small` | `cpx22` / `cpx22` / `cpx32` |
| `medium` | `cpx22` / `cpx42` / `cpx42` |
| `medium-optimized` | `cpx22` / `cpx32` / `cpx32` |
| `production` | `cpx22` / `cpx42` / `cpx42` |

The minimal 4-vCPU/8-GiB node floor and the medium-optimized seven-node
failure-domain layout come from the completed live campaign. The balanced CPX
default retains the tested 4-vCPU/8-GiB node envelope. The CX mapping uses
`cx33` control planes and intentionally raises workers to `cx43`: quorum nodes
stay economical while workload capacity receives the larger shape.

## Five-profile price balance

These totals include servers, one bastion Primary IPv4, the profile's load
balancer where enabled, and every billable CSI volume selected by the named
base profile. They exclude opt-in service claims, external DR object storage,
snapshots, excess traffic,
domains, support, and non-zero customer VAT.

| Profile | CSI / active local claims | CX economy | CAX planning | CPX balanced | CCX dedicated |
|---|---:|---:|---:|---:|---:|
| `minimal` | 250 / 0 GiB | €37.27 | €41.77 | **€105.27** | €229.77 |
| `small` | 360 / 0 GiB | €56.54 | €61.54 | **€138.54** | €286.54 |
| `medium` | 1,410 / 0 GiB | €174.08 | €199.58 | **€455.58** | €561.58 |
| `medium-optimized` | 260 / 410 GiB | €117.78 | €102.28 | **€290.78** | €667.78 |
| `production` | 1,410 / 0 GiB | €190.07 | €220.57 | **€525.07** | €647.57 |

CX figures are valid purchase prices whenever the complete mapping reappears;
the required shapes were temporarily not placeable in `hel1` at capture time.
Selecting CX never silently falls back to CPX, because that would change the
approved cost. Retry later or choose CPX explicitly. CAX figures are not an
approved production deployment. CPX is the current default and was placeable.
CCX is available but should be chosen for CPU predictability, not storage
savings.

`medium-optimized` is a resource-envelope variant of the base medium toolset,
not a linear tier between `medium` and `production`. GitLab/Runner and
PostgreSQL are mandatory from `small` upward. MongoDB, Temporal, Postal, and
GlitchTip are opt-in. Medium-optimized
uses more small nodes than production, so CCX can make it more expensive than
the six-node production topology.

### Medium-optimized CX resource balance

The CX option is intentionally mixed instead of assigning `cx33` everywhere:

| Role | Shape and count | vCPU | RAM | Node-local SSD | CSI capacity | Monthly net |
|---|---|---:|---:|---:|---:|---:|
| Schedulable control planes | 3 × `cx33` | 12 | 24 GiB | 240 GiB | — | €25.47 |
| Workers | 4 × `cx43` | 32 | 64 GiB | 640 GiB | — | €63.96 |
| Bastion | 1 × `cx23` | 2 | 4 GiB | 40 GiB | — | €5.49 |
| `lb11` and bastion IPv4 | 1 each | — | — | — | — | €7.99 |
| **Infrastructure** | | **46** | **92 GiB** | **920 GiB** | — | **€102.91** |
| Provider CSI volumes | 18 volumes | — | — | — | 260 GiB | €14.87 |
| Active local PVC claims | 20 volumes | — | — | 410 GiB | — | included |
| Expandable static local pool | 23 slots | — | — | 470 GiB | — | included |
| **Total base claims** | | | | **410 GiB** | **260 GiB** | **€117.78** |

Excluding the bastion, Kubernetes receives 44 vCPU, 88 GiB RAM, and 880 GiB
aggregate node-local SSD. Relative to four `cx33` workers, the four `cx43`
workers double the workload pool from 16 to 32 vCPU, 32 to 64 GiB RAM, and 320
to 640 GiB local SSD for €30/month more. The three control planes remain
schedulable and contribute another 12 vCPU and 24 GiB RAM. Of the Kubernetes
nodes' 880 GiB aggregate SSD, base claims actively select 410 GiB and the
pre-created pool exposes up to 470 GiB for the late MongoDB opt-in.

## Local disk and volume boundary

The SSD column is node-local root storage, not shared storage. The
`medium-optimized` profile now uses it selectively through a capacity-aware
23-volume static local PV pool, `WaitForFirstConsumer`, retained PVs,
required hostname anti-affinity, minimum root-disk gates of 70 GiB on control
planes and 140 GiB on workers, and a 40 GiB per-node free-space gate. Only
SeaweedFS master/volume/index, Vault Raft data, and Elasticsearch master/data
claims and PostgreSQL use this class in the base profile. Explicitly enabled
MongoDB also uses it because the application replicates across nodes.

Node-local PVs remain pinned to their node. A failed or deleted node does not
carry its local PV to a replacement. Application quorum repairs the live
service and external native plus Velero/Kopia backups provide recovery.
PV capacity is a Kubernetes scheduling reservation, not a hard per-directory
filesystem quota; all local slots share the node root filesystem and remain
protected by the 85% disk-usage alert plus kubelet `DiskPressure`.
SeaweedFS filer, Vault audit, pgBackRest, observability, Gitaly, Dragonfly,
Coroot/ClickHouse, Tempo, and GitLab backup staging remain on Hetzner CSI.
Existing CSI claims cannot change StorageClass in place; migrate them only
through the backup-gated replacement/native-restore procedure.
