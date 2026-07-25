# Hetzner capacity tariffs

This document records the authenticated Hetzner Cloud catalog observed for
`hel1` on 2026-07-24. The repository does not treat this snapshot as a placement
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

## Telegram capacity monitor

The stateful monitor checks every location whose provider-reported country is
in the European Union. It tracks all three shapes required by the
`medium-optimized` CX mapping: `cx23` for the bastion, `cx33` for three control
planes, and `cx43` for three workers. Telegram reports partial availability,
complete deployable availability, and capacity loss. Each message lists the
available and missing shapes plus the exact transition.

Add the bot credentials to the protected, gitignored `.env`. The monitor can
reuse the Alertmanager destination:

```dotenv
ALERT_TELEGRAM_BOT_TOKEN=123456:replace-with-botfather-token
ALERT_TELEGRAM_CHAT_ID=-1001234567890
```

Or isolate capacity notifications with
`CX_CAPACITY_TELEGRAM_BOT_TOKEN` and
`CX_CAPACITY_TELEGRAM_CHAT_ID`. The dedicated variables take precedence.
Keep `.env` mode `0600`.

Test Telegram delivery without querying Hetzner, then run one authenticated
capacity check:

```bash
./scripts/notify-cx-capacity-telegram.sh --test-telegram
./scripts/notify-cx-capacity-telegram.sh
```

Use `--dry-run` to query and render a pending notification without sending it
or modifying monitor state. Normal runs persist non-secret state at
`platform-orchestrator/.state/cx-capacity-monitor.json`; that directory is
gitignored and mode `0700`, while the state and lock files are mode `0600`.
The state records successful delivery separately from observed capacity, so a
Telegram failure is retried and capacity that disappears and later returns
notifies again. Partial and complete states are delivered once per transition;
unchanged capacity is silent.

The message includes the exact 3+3 target, available and missing shapes,
infrastructure and volume split, local-claim reservation, and current net
monthly total. A partial report is informational and is not permission to
deploy. In its default notification-only mode, the monitor never creates,
resizes, or deletes resources. Re-run the location-specific report and set
`infrastructure.region` to the reported location before manual provisioning;
availability can disappear between the notification and server creation.

### Optional one-shot automatic deployment

Automatic deployment is disabled by default. Enable it only after the protected
`.env` contains `HCLOUD_TOKEN`, the DR credentials, the GitLab Runner token,
and `ANSIBLE_VAULT_PASSWORD_FILE`:

```dotenv
CX_CAPACITY_AUTO_DEPLOY=true
CX_CAPACITY_DEPLOY_PROJECT=n0xeid-medium-optimized-cx
CX_CAPACITY_DEPLOY_DOMAIN=n0xeid.xyz
CX_CAPACITY_DNS_ZONE=n0xeid.xyz
CX_CAPACITY_MANAGE_DNS=true
CX_CAPACITY_CERTIFICATE_ISSUER=letsencrypt-prod
CX_CAPACITY_DEPLOY_RETRY_SECONDS=300
CX_CAPACITY_DEPLOY_STALE_SECONDS=900
```

When a location becomes `COMPLETE`, the monitor selects one deterministic EU
location and immediately performs a second authenticated capacity query. It
does nothing if any required shape disappeared. If the second gate passes, it
executes the equivalent of:

```bash
./run_tier.sh medium-optimized \
  --campaign-id cx-auto \
  --project n0xeid-medium-optimized-cx \
  --domain n0xeid.xyz \
  --location LOCATION \
  --capacity-family cx \
  --dns-zone n0xeid.xyz \
  --certificate-issuer letsencrypt-prod \
  --manage-dns
```

The location override is written into the generated desired-state profile and
the campaign status. The stable project, run root, operator state, and playbook
inputs make retries reconcile the same cluster instead of creating another
one. State is marked `running`, `failed`, or `succeeded`; a successful
deployment is never launched again. A failed attempt retries after the
configured backoff. A persisted `running` attempt is not reclaimed until its
stale timeout, which prevents a second scheduler process from racing an active
campaign. Telegram receives start, failure, and success messages. Console
output is retained in the protected campaign directory.

This automation purchases billable cloud resources. Disabling the flag stops
future starts but does not delete an existing cluster. Use the reviewed
teardown workflow for deletion.

`CX_CAPACITY_DEPLOY_DOMAIN=n0xeid.xyz` intentionally uses the zone apex. The
Hetzner DNS role converges `@`, `*`, and `vpn` records, so the platform root is
`n0xeid.xyz` and individual services use names below it. Before the first
deployment, change this variable and `CX_CAPACITY_DNS_ZONE` freely and validate
with `run_tier.sh --dry-run`. After deployment, changing the domain requires a
planned migration of DNS records, certificates, Gateway/Ingress hosts, GitLab
external URLs and callbacks, application configuration, and verification; it
must not be treated as an in-place rename.

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

The minimal 4-vCPU/8-GiB node floor comes from the completed live campaign.
The current medium-optimized layout is a reviewed 3+3 Kubernetes topology.
The balanced CPX
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
| `medium-optimized` | 240 / 300 GiB | €100.65 | €90.65 | **€254.15** | €580.65 |
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
| Workers | 3 × `cx43` | 24 | 48 GiB | 480 GiB | — | €47.97 |
| Bastion | 1 × `cx23` | 2 | 4 GiB | 40 GiB | — | €5.49 |
| `lb11` and bastion IPv4 | 1 each | — | — | — | — | €7.99 |
| **Infrastructure** | | **38** | **76 GiB** | **760 GiB** | — | **€86.92** |
| Provider CSI volumes | 17 volumes | — | — | — | 240 GiB | €13.73 |
| Active local PVC claims | 15 volumes | — | — | 300 GiB | — | included |
| Expandable static local pool | 18 slots | — | — | 360 GiB | — | included |
| **Total base claims** | | | | **300 GiB** | **240 GiB** | **€100.65** |

Excluding the bastion, Kubernetes receives 36 vCPU, 72 GiB RAM, and 720 GiB
aggregate node-local SSD. The three `cx43` workers provide 24 vCPU, 48 GiB RAM,
and 480 GiB local SSD. The three control planes remain schedulable and
contribute another 12 vCPU and 24 GiB RAM. Of the Kubernetes nodes' 720 GiB
aggregate SSD, base claims actively select 300 GiB and the pre-created pool
exposes up to 360 GiB for the late MongoDB opt-in.

## Local disk and volume boundary

The SSD column is node-local root storage, not shared storage. The
`medium-optimized` profile now uses it selectively through a capacity-aware
18-volume static local PV pool, `WaitForFirstConsumer`, retained PVs,
required hostname anti-affinity, minimum root-disk gates of 70 GiB on control
planes and 140 GiB on workers, and a 40 GiB per-node free-space gate. Only
SeaweedFS master/volume/index, Vault Raft data, and PostgreSQL use this class
in the base profile. Explicitly enabled
MongoDB also uses it because the application replicates across nodes.

Node-local PVs remain pinned to their node. A failed or deleted node does not
carry its local PV to a replacement. Application quorum repairs the live
service and external native plus Velero/Kopia backups provide recovery.
PV capacity is a Kubernetes scheduling reservation, not a hard per-directory
filesystem quota; all local slots share the node root filesystem and remain
protected by the 85% disk-usage alert plus kubelet `DiskPressure`.
SeaweedFS filer, Vault audit, pgBackRest, observability, Gitaly, Dragonfly,
Coroot/ClickHouse, Loki, and GitLab backup staging remain on Hetzner CSI.
Tempo remains off unless explicitly selected.
Existing CSI claims cannot change StorageClass in place; migrate them only
through the backup-gated replacement/native-restore procedure.
