# Deployment Guide

## 1. Prepare the control machine

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
bash scripts/validate-local.sh
```

Install `hcloud`, `kubectl`, Helm, `yq`, `jq`, and `ssh`. Create an Ed25519
key if one does not already exist.

```bash
umask 077
openssl rand -base64 48 > ~/.vault_pass
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Set `ANSIBLE_VAULT_PASSWORD_FILE`, `HCLOUD_TOKEN`, and any provider/backup
credentials in `.env`. All supported operational scripts automatically load
this gitignored file; an explicitly exported process variable takes precedence.
The loader rejects symlinks, insecure permissions, malformed assignments, and
does not evaluate shell commands from `.env`.

The deployment stops if the Vault password file, SSH key, CLI, pinned Ansible
collection, token, domain, or email is missing.

## 2. Select and review a profile

```bash
cd platform-orchestrator
./platform.sh init medium
$EDITOR platform.yaml
```

Available profiles are `minimal`, `small`, `medium`, `medium-optimized`, and
`production`. Set `global.domain` and `global.email`; review server counts,
regions, storage, backup, and every component `enabled` flag.

Parallel controller runs require a separate worktree and `HOME`, unique
`global.project`, and unique `k8s_api_local_port` for each cluster. This keeps
kubeconfig, API tunnel PID/listener, known-hosts, Ansible facts, Kubespray
control sockets, and temporary manifests isolated. If the cluster domain is a
subdomain of an existing Hetzner zone, set top-level `hetzner_dns_zone` to
that parent instead of creating an undelegated child zone.

There are four runtime tiers. `medium-optimized` is intentionally a named
profile rather than a fifth tier: it sets `tier: medium` for the medium
foundation and `resource_tier: small` for the compact resource envelope, then
explicitly removes overlapping optional observability backends.
GitLab/Runner and PostgreSQL are mandatory from `small` upward;
MongoDB, Temporal, Postal, and GlitchTip remain explicit opt-ins.
Do not rename it to `medium_optimized` or change its tier to `small`; both would
break the explicit profile contract and are rejected before provisioning.

For the budget production-oriented deployment:

```bash
./platform.sh init medium-optimized
```

This profile uses three schedulable `cpx32` control planes, three `cpx32`
workers, a `cpx22` bastion, and `lb11`. Critical quorum services remain
replicated;
recoverable stateless services default to one replica with bounded autoscaling.
VictoriaMetrics storage, Gitaly, Grafana, Loki, and Coroot/ClickHouse retain
documented singleton recovery boundaries.
Coroot receives application traces through one OpenTelemetry Collector.
Elasticsearch/Kibana/APM, PMM, and Tempo remain selectable but are off in this
profile because they overlap with the Coroot-centric baseline.
Choose the `production` profile when stateless workload continuity during node
maintenance is required. Production backups must also be copied to storage
outside this cluster.

At the authenticated Hetzner API prices audited on 2026-07-30, the complete
currently placeable balanced shape is €357.984/month net (€357.98 rounded),
including isolated `cpx32` Docker and `cpx42` general/image-build workers,
bastion IPv4, 300 GiB of active
replication-qualified local claims in a
450 GiB expandable static pool, and 220 GiB
of billable CSI volumes. Its six-node platform base without the CI worker is
€253.00/month. GitLab backup staging uses transient node SSD before its immediate
object-storage upload. The intermittent CX cost-optimized platform base is
€99.50/month whenever all required shapes are
placeable: three `cx33` control planes retain economical quorum capacity while
three `cx43` workers provide 24 vCPU, 48 GiB RAM, and 480 GiB worker-local SSD.
It was temporarily unavailable for new `hel1`
placement at audit time. The local portion contains only data with
application-level replication across nodes. Singleton, audit, and backup
claims remain on CSI. External DR storage and traffic overages are separate;
see [the cost model](docs/COST_MODEL.md) and
[Hetzner capacity tariffs](docs/HETZNER_CAPACITY_TARIFFS.md).

The current live deployment uses the CX platform base plus isolated `cpx32`
Docker and `cpx42` general/image-build workers. At the current authenticated
prices, the resulting footprint is **€204.48/month net**, including 220 GiB of
provider volumes. Both workers are private, tainted, excluded from ingress and
local PVs, and required to keep CI disk pressure away from production.

Its three SeaweedFS volume servers use placement `001`, not SeaweedFS's unsafe
single-copy `000` default. Reconciliation upgrades old volumes in bounded
batches, verifies that no volume is under-replicated, and refreshes the filer
after Raft/chart topology changes. Loki PVC auto-deletion is disabled for every
deployment mode, so an operator scale-to-zero cannot silently delete log data.

GitLab chart 10 cannot be enabled without PostgreSQL, Dragonfly, and object
storage. The normalizer rejects that invalid profile before infrastructure is
changed.

Inspect and change the selected technologies through the orchestrator:

```bash
./platform.sh components
./platform.sh enable gitlab       # enables storage, PostgreSQL, and Dragonfly
./platform.sh enable temporal     # optional; enables PostgreSQL
./platform.sh enable postal       # optional; enables Dragonfly
./platform.sh enable glitchtip    # optional; enables PostgreSQL + Dragonfly
./platform.sh disable daytona
./platform.sh validate
```

`enable` adds required dependencies. `disable` refuses when another enabled
technology still depends on the target. The validated dependency graph also
covers ESO -> secrets, database engines -> databases, Runner -> GitLab,
GlitchTip -> PostgreSQL + Dragonfly, APM -> Elasticsearch, Temporal ->
PostgreSQL, Postal -> Dragonfly, tracing -> observability + storage, Blackbox ->
observability, and backup -> storage. Metrics, logging, and Grafana are
intentionally deployed as one observability core bundle. PMM is an independently
selectable dependant of that bundle.
Coroot -> observability is also enforced; HIPAA-oriented hardening requires
secrets, observability, Cilium encryption, and active log redaction. The full
selector, profile matrix, and removal classes are in the
[technology catalog](docs/TECHNOLOGY_CATALOG.md).
Adding a selector later reuses the existing foundation, but it does not create
capacity. Validate allocatable CPU, memory, storage, and topology constraints
before reconciliation; use the supported named-profile migration or approved
node-resize workflow when the current cluster cannot place the added workload.
Alert delivery channels remain settings under `alerting.telegram.enabled` and
`alerting.email.enabled`; email requires Postal, while Telegram also requires
`ALERT_TELEGRAM_BOT_TOKEN` and `ALERT_TELEGRAM_CHAT_ID` at deployment time.
Postal deployment is schema-gated: a fresh MariaDB runs `postal initialize`,
an existing database runs `postal update`, and web/worker/SMTP processes are
not reconciled until that Job completes. This path runs only after Postal is
explicitly enabled. SMTP stays public on ports 25/587 but uses unprivileged
container port 2525.

Validate the selected profile without contacting Hetzner or Kubernetes:

```bash
ansible-playbook playbooks/validate_profile.yml \
  -e @platform-orchestrator/profiles/medium-optimized.yaml
```

## 3. Deploy

```bash
./platform.sh deploy all
```

The full flow creates Hetzner resources, configures the bastion/network,
installs Kubernetes, then deploys only enabled services. Firewall and load
balancer policies converge on rerun. Server removal and type changes always
fail closed in the infrastructure role. Use the resumable profile migration
workflow for those operations so drain, PDB, CSI, disk-pressure, and etcd
quorum gates run one node at a time.

Component runs are available when recovery or maintenance requires them:

```bash
./platform.sh deploy infra
./platform.sh deploy network
./platform.sh deploy dns
./platform.sh deploy cluster
./platform.sh deploy tls
./platform.sh deploy secrets
./platform.sh deploy eso
./platform.sh deploy object-storage
./platform.sh deploy databases
./platform.sh deploy postgresql
./platform.sh deploy mongodb
./platform.sh deploy elasticsearch
./platform.sh deploy dragonfly
./platform.sh deploy gitlab
./platform.sh deploy gitlab-runner
./platform.sh deploy gitops
./platform.sh deploy observability
./platform.sh deploy pmm
./platform.sh deploy coroot
./platform.sh deploy tracing
./platform.sh deploy autoscaling
./platform.sh deploy temporal
./platform.sh deploy postal
./platform.sh deploy backup
./platform.sh deploy disaster-recovery
./platform.sh deploy glitchtip
./platform.sh deploy apm
./platform.sh deploy blackbox
./platform.sh deploy daytona
./platform.sh deploy hipaa
```

Every targeted deployment always runs profile normalization and dependency
validation first. It fails if the component is disabled; enable it explicitly
instead of overriding an Ansible variable on the command line.

`deploy pmm` reconciles the optional Percona Monitoring and Management server;
database operators enable their PMM clients only while this selector is on.
It is disabled by default for the constrained `minimal` and `small` profiles.
`deploy coroot` reconciles the observability bundle plus the pinned official
Coroot operator/CE resources. Its eBPF node agent requires privileged Pod
Security admission, scoped only to the `coroot` namespace. `deploy hipaa`
reconciles network host controls and every selected log collector so redaction
is active rather than an unused configuration object.

`deploy backup` installs application-native backup automation.
`deploy disaster-recovery` additionally reconciles external Velero/Kopia and
requires the independent S3 endpoint, bucket, and credentials. Selecting
`disaster-recovery` through the CLI enables native backup and object storage as
dependencies; native backup cannot be disabled while the external layer is
selected. Both targeted deploy commands also reconcile the Percona and GitLab
owners of their schedules, so enabling backup later does not require a full
platform deployment. After disabling `backup`, guarded removal deletes its
CronJobs, removes PostgreSQL pgBackRest schedules, and disables MongoDB backup,
PITR, and scheduled tasks. Database data, repositories, PVCs, buckets, and
already-created backup objects remain intact.

To stop selecting a component now but preserve the easiest return path, disable
it and leave its resources in place. Re-enable and reconcile it later. To free
cluster resources, disable it first and then use the guarded removal workflow:

```bash
./platform.sh disable blackbox
./platform.sh remove blackbox --confirm blackbox

# Stateful example: verify backups before authorizing PVC/namespace deletion.
./platform.sh disable temporal
./platform.sh remove temporal --confirm temporal --delete-data
```

Removal never deletes Hetzner infrastructure, DNS, remote backup objects, or
the tracing bucket. Data-bearing components refuse removal without
`--delete-data`. Re-enabling after a non-removal pause reconciles the retained
installation; re-enabling after data deletion creates a fresh service unless
you perform the documented restore procedure. Re-enable never automatically
selects or replays a retained backup.

HIPAA-oriented hardening can be disabled to stop future reconciliation, but
generic removal is refused because host and cluster security controls cannot
be safely reversed without an organization-specific, reviewed change plan.

Direct Ansible invocation uses the real example inventory:

```bash
cp inventory.example inventory.yml
$EDITOR inventory.yml
ansible-playbook -i inventory.yml playbooks/deploy_platform.yml
```

## 4. Verify

```bash
kubectl get nodes
kubectl get pods -A
helm list -A
./scripts/health-gates.sh
./platform-orchestrator/platform.sh status
```

If backups are enabled, verify the jobs and immediately run a backup:

```bash
kubectl get cronjob -A
./scripts/backup-all.sh --force
kubectl get backupstoragelocation -n velero
```

GitLab uses the chart-generated `gitlab-toolbox-backup` CronJob and a separate
`gitlab-rails-secrets-backup` job. Both are required for recovery.
`medium`, `medium-optimized`, and `production` also require an independent
external DR endpoint and credentials before the backup role can install
Velero/Kopia. The deployment preflight rejects missing or in-cluster values
before provisioning Hetzner resources. The platform CLI loads the mode-`0600`,
gitignored project `.env`; blank named-profile endpoint and bucket fields use
`BACKUP_DR_ENDPOINT` and `BACKUP_DR_BUCKET`, while credentials remain
environment-only and never appear in Ansible argv. After deployment, create an encrypted full-cluster bundle with
`platform.sh backup-cluster`; when Vault is enabled, pass its exact encrypted
initialization file with `--vault-init-file`. See
[BACKUP_RESTORE.md](BACKUP_RESTORE.md).

Do not replace one named profile with another and run `deploy all`. Use
`platform.sh migrate --target PROFILE plan|execute`, followed by
`resume|status|rollback|finalize`. All 20 distinct transitions among `minimal`,
`small`, `medium`, `medium-optimized`, and `production` use the same external
backup gate. The workflow expands to the larger source/target topology first,
resizes retained nodes one at a time, grows the provider disk and root
filesystem, checks etcd around every control-plane change, unseals Vault after
each restart, and requires the full profile-aware health gate before moving to
the next node. Scale-in and source-data deletion remain behind `finalize`.
Equivalent-compute server types with a larger existing root disk are retained
and written into the active migration config; a disk-shrinking compute change
fails before mutation instead of attempting an unsafe provider resize.

VictoriaMetrics topology changes carry a separate data proof. Migration writes
a deterministic one-hour historical sentinel, requires the exact value and
millisecond timestamp from source and destination, binds that result to the
migration descriptor, and re-queries the live destination before retiring the
old resource and again immediately before deleting its PVCs. Rollback first
copies post-switch samples back and proves an exact delta sentinel on both
sides. A completed copy Job by itself is never deletion authority. Before every
finalization invocation with destructive stages pending, the workflow also
refreshes and verifies the final encrypted recovery point; Velero is removed
only near the end when the target disables scheduled backup.

Downgrades never request an in-place PVC shrink because Kubernetes storage
cannot safely do that. The generated named target records larger existing
requests as explicit overrides, while finalization removes obsolete service
PVCs, old VictoriaMetrics/Loki topology, excess nodes, a disabled load
balancer, and an unused spread placement group. Unsafe SeaweedFS, Vault Raft,
and same-topology VMCluster replica reductions are also retained explicitly
until a service-specific compaction/member-removal window.

## 5. Application delivery

This repository installs Argo CD when `gitops.enabled` is true but does not
register an application repository automatically. Create an Argo CD
Application/AppProject in your application repository and keep it inside the
configured source, namespace, and resource allowlists.

## 6. Teardown

```bash
./platform-orchestrator/platform.sh destroy
```

Type the exact confirmation when prompted. The script selects load balancers,
servers, firewalls, and labeled placement groups only when their
`project` label exactly equals the confirmed project; overlapping name prefixes
never broaden deletion scope. It also handles the exact conventional SSH-key,
subnet, network, and legacy `${project}-spread` names. Before server deletion it
captures every provider volume attached to those exact labeled servers,
including CSI-generated `pvc-*` names. It captures detached/retained Hetzner
CSI volume handles from Kubernetes PVs only when every node in the active
context is an exact member of that provider-server set. It then detaches,
deletes, and verifies those exact volume IDs. It intentionally preserves DNS
and the global kubeconfig.
