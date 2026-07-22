# Ansible Kubernetes Platform for Hetzner Cloud

This repository provisions a Kubernetes platform on Hetzner Cloud and installs
the selected platform services. It is infrastructure automation; it does not
ship or deploy an application repository by default.

## What it manages

- Hetzner network, firewalls, bastion, control-plane and worker servers, load
  balancer, and platform DNS records.
- Kubernetes v1.35.4 with Cilium, Gateway API, cert-manager, Hetzner CCM/CSI,
  and a private-node topology.
- Profile-controlled services: SeaweedFS; Vault and External Secrets Operator;
  Percona PostgreSQL and MongoDB; Elasticsearch/Kibana; Dragonfly; GitLab and
  Runner; Argo CD; VictoriaMetrics, Grafana, Loki/ELK, PMM, Alertmanager,
  Coroot, Tempo/OpenTelemetry, and Blackbox Exporter; KEDA; Temporal; Postal;
  GlitchTip; Elastic APM; Daytona; backup automation; and HIPAA-oriented
  technical hardening. See the exhaustive
  [technology catalog](docs/TECHNOLOGY_CATALOG.md).
- Native application backups, external Velero/Kopia resource and PVC backups,
  encrypted etcd/PKI/config bundles, restore drills, staged upgrades, exact
  Helm rollback baselines, and verified teardown.

The runtime has four capability tiers and five named profiles:

| Profile | Capability tier | Resource tier | Default topology | Service scope |
|---|---|---|---|---|
| `minimal` | minimal | minimal | 1 schedulable 4 vCPU/8 GiB control plane + 1 4 vCPU/8 GiB worker | Core development platform; no GitLab or medium-only services |
| `small` | small | small | 1 control plane + 2 workers | Compact GitLab platform with PostgreSQL, Dragonfly, storage, secrets, GitOps, and monitoring |
| `medium` | medium | medium | 3 schedulable control planes + 2 workers | Full platform with standard medium sizing; control-plane capacity is part of the workload envelope |
| `medium-optimized` | medium | small | 3 schedulable control planes + 4 workers | Full medium service set with conservative requests, replicas, retention, and autoscaling |
| `production` | production | small | 3 tainted control planes + 3 workers | Selective critical HA with explicit quorum/workload replicas, failover headroom, and grow-only storage defaults |

The current audited `medium-optimized` Hetzner shape is approximately
**€115.81/month net** at the 2026-07-22 API prices: seven `cx33` nodes, one
`cx23` bastion, `lb11`, one bastion IPv4, and 750 GiB of billable volumes. The
volume total includes two Elasticsearch data replicas, three separately
persistent SeaweedFS index claims, and 20 GiB of GitLab backup staging. See the
exact arithmetic, hourly boundary, and exclusions in
[the cost model](docs/COST_MODEL.md).

`tier` controls which capabilities are installed. `resource_tier` controls
default pod requests, limits, and stateless replica counts. This separation is
what lets `medium-optimized` retain the full medium toolset without silently
allocating the normal-medium footprint. Production uses the same conservative
request envelope and pins critical HA replicas explicitly. Its control planes
are tainted for general workloads, while the critical PostgreSQL, MongoDB, and
Elasticsearch stateful replicas tolerate those taints so a single worker loss
does not exhaust the remaining workers or their volume-attachment capacity.
GitLab chart 10 requires PostgreSQL,
Dragonfly, and object storage; profile validation rejects an invalid
combination. The same fail-closed validation covers GlitchTip, APM, Temporal,
Postal, Coroot, tracing, backup, HIPAA-oriented hardening, ESO, the GitLab
Runner, and parent bundles.

Production is deliberately selective HA, not universal active-active HA. Its
control plane, Vault, SeaweedFS masters/volumes, PostgreSQL, MongoDB, GitLab
Rails workloads, Argo CD, VictoriaMetrics, Temporal, alerting, tracing, and
selected stateless services have explicit redundant topology. The compact
footprint retains these intentional singleton recovery boundaries:

- GitLab Gitaly uses one RWO data claim. Recover it from the verified GitLab
  application backup and cluster/PVC backup before reopening repository writes.
- Elasticsearch has three masters but one data node. It has no data-replica
  failover; restore its data claim from a verified cluster backup.
- Grafana uses one SQLite/RWO instance. Reapply declarative data sources and
  dashboards and restore its claim when local UI state must be retained.
- Postal has one web instance and one MariaDB data path. Redeploying the web
  pod is stateless, but MariaDB must be restored before mail operations resume.
- Coroot and its ClickHouse data path are singletons. Restore their claims when
  historical telemetry is required, or explicitly accept a telemetry rebuild.

These boundaries reduce steady-state requests on three 16 GiB workers; backup
and restore gates provide recovery, not instant failover, for the listed data
paths.

The optimized profile keeps three-way control-plane, Vault, PostgreSQL,
MongoDB, SeaweedFS, and Elasticsearch-master topology. Recoverable stateless
services run one replica by default and autoscaling is capped at four. It is a
production-oriented budget profile, but it does not provide the same workload
availability during maintenance as the `production` profile. Store production
backups outside the cluster for disaster recovery.

Every multi-volume SeaweedFS profile uses replica placement `001`: each object
volume has a second copy on another volume server. Existing `000` volumes are
migrated and verified during reconciliation, and the filer is refreshed after
master/Raft topology changes so S3 reads cannot retain a stale volume map.
Loki StatefulSet claims are always retained on scale-down and release deletion;
profile finalization archives and removes them only behind migration backup
gates.

## Safety model

- Secret files are encrypted with Ansible Vault. There is no automatic
  plaintext fallback.
- Vault uses internal TLS by default. Initialization material is written only
  to an encrypted local file; unseal keys are not stored in a Kubernetes
  Secret or CronJob.
- Server count/type drift fails until destructive reconciliation is explicitly
  authorized. Firewall, load-balancer service/target, and DNS drift converges.
- Upgrades stop on failed preflight, backup, Helm, migration, or health gates.
  Rollback uses captured exact revisions rather than `revision - 1` guesses.
- Profile changes use the resumable migration workflow; the ordinary upgrade
  command rejects cross-tier changes so topology and data transitions cannot
  be mistaken for a Helm reconcile.
- Teardown requires the exact project name and verifies that every managed
  Hetzner resource was removed. DNS and the global kubeconfig are preserved.
- HIPAA-related controls are opt-in technical hardening only; enabling them is
  not a compliance certification.

## Prerequisites

- Python 3.12 or newer
- Ansible Core (installed through `requirements.txt` via `ansible-lint`)
- `hcloud`, `kubectl`, Helm, `yq`, `jq`, and `ssh`
- an existing Ed25519 SSH key
- a Hetzner Cloud API token
- a dedicated Ansible Vault password file

Install repository dependencies:

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
```

Create the local secret-encryption password file and repository-local
environment configuration:

```bash
umask 077
openssl rand -base64 48 > ~/.vault_pass
cp .env.example .env
chmod 600 .env
$EDITOR .env
```

Set `ANSIBLE_VAULT_PASSWORD_FILE`, `HCLOUD_TOKEN`, and the required backup
credentials in `.env`. The gitignored file is automatically loaded by the
orchestrator, deployment, teardown, migration, backup, restore, upgrade, and
restore-drill scripts. Explicit variables exported by the caller take
precedence, which keeps CI and one-off overrides deterministic.

Object-storage credentials may be supplied through
`OBJECT_STORAGE_ACCESS_KEY` and `OBJECT_STORAGE_SECRET_KEY`; otherwise the
secret-generation role creates and persists strong values in the encrypted
platform secrets file.

## Quick start

The orchestrator is the canonical entry point:

```bash
cd platform-orchestrator
./platform.sh init medium
$EDITOR platform.yaml
./platform.sh deploy all
```

For concurrent clusters, use a separate worktree and `HOME` for each controller
process, a unique `global.project`, and a unique `k8s_api_local_port`. The
controller keeps kubeconfig, SSH trust, Ansible facts, and outer SSH sockets
under that `HOME`; Kubespray sockets and downloaded manifests are also scoped
by project. `KUBECONFIG` alone is not sufficient because the playbooks
deliberately use `$HOME/.kube/config`.

The disposable five-profile campaign runner prepares those boundaries and
launches every named profile concurrently from one immutable commit. Use the
minimum-storage switch only to stay inside a test account's CSI quota; it keeps
the real topology, replicas, components, and compute node types while reducing
profile-controlled durable PVC requests to Hetzner's 10Gi minimum. Vault audit
claims follow the selected Vault size instead of a hidden 20Gi floor. SeaweedFS
data and indexes remain durable on the same CSI data claim. GitLab backup
staging uses pod-local scratch in this explicit campaign mode;
the completed backup remains durable in S3 and normal deployments keep
persistent staging enabled by default. During SeaweedFS conversion,
the reconciler writes a pre-migration S3 sentinel, checkpoints its stage,
quiesces filers and volume servers, atomically copies and compares each index,
then restores service and proves both pod restarts and the pre-existing S3
object before deleting exact obsolete index PVCs. Test certificates default to
`letsencrypt-staging` so repeated campaigns do not consume the registered-domain
production issuance limit.

Live profile migration also fails closed on Hetzner volume capacity. The plan
derives billable per-component source/target claims and backup scratch; execute
requires the explicit account GiB quota because Hetzner has no quota API, then
persists the authoritative volume baseline and rechecks remaining capacity on
resume with a configurable safety margin.

Migration never changes the bastion type in place. It reads the authoritative
live Hetzner type, retains it in every generated config, and records the
declared, requested, and retained values in resumable state. This also repairs
state created from a deployment whose bastion override existed only on the
command line. Expansion creates or reconciles its spread placement group with
the exact `project=<global.project>` ownership label.

Each retained-node resize waits for the authoritative Hetzner server state to
be `off` before placement reconciliation and checks for `off` again immediately
before changing type. This makes a retry safe when an interrupted run leaves a
node cordoned or when a placement-group operation returns the server to
`running`; `resume` completes that node before advancing to the next one.

The backup checkpoint captures its Helm rollback baseline with the migration's
exact generated source config, not the repository default. Both the config and
snapshot root are passed explicitly, and the baseline is retained under the
durable per-migration state directory so `resume` and `rollback` use the same
isolated controller state.

Public ingress follows the Cilium-owned Gateway Service instead of mutating
that controller-owned object. After Cilium's reconciliation loop settles, the
cluster role discovers its live HTTP/HTTPS NodePorts, converges the Hetzner
load-balancer destination and health-check ports, and fails until every target
is healthy. The load-balancer-free `minimal` tier reuses its bastion: HAProxy
keeps `vpn.<domain>` on Caddy/Headscale and passes all other HTTP/TLS traffic to
the same discovered Gateway ports. This preserves the small resource envelope
without leaving the tier's public DNS disconnected from Kubernetes.

If Hetzner reports `resource_unavailable` for the default `cx` pool, add
`--capacity-family cpx`. The runner substitutes `cpx22`, `cpx32`, and `cpx42`
at the same 2/4, 4/8, and 8/16 vCPU/GiB floors. Minimal uses `cpx32` for both
Kubernetes nodes: live full-recovery testing proved that 2/4 nodes cannot keep
the core stack and one Velero node agent per node schedulable. It does not change node counts,
HA, replicas, or enabled technologies. Explicit per-controller
`--bastion-type`, `--cp-type`, and `--worker-type` overrides are also available
and are rejected by the infrastructure role if they fall below a profile's
capacity floor.

After logs prove Kubespray completed with zero failed or unreachable hosts, a
campaign interrupted in later platform roles can resume with
`--skip-kubespray`. The resume still reconciles infrastructure, networking,
CCM/CSI, every selected platform component, and readiness gates; it only omits
the already-successful Kubespray `cluster.yml` run. Never use the flag when the
Kubespray recap is missing or failed.

Five-way campaigns default to one Ansible fork per isolated controller. This
keeps every tier active concurrently while bounding controller RAM and process
fan-out; raise it explicitly with `--controller-forks` only when the controller
host has measured headroom.

The runner stores encrypted platform credentials and Vault initialization
material under the main checkout's gitignored `.campaign-state/PROJECT/`
directory. Fresh isolated worktrees and `--skip-kubespray` resumes reuse that
protected state instead of silently generating new credentials. Back up this
directory with the vault password; an initialized Vault intentionally refuses
reconciliation when its protected state is missing.

```bash
# Plan all five controllers without Git worktrees or cloud mutations.
./run_all.sh --campaign-id lab01 --minimum-storage --dry-run

# Create an independent DR target with disposable compute and durable storage,
# then deploy all five profiles.
eval "$(./scripts/test-dr-endpoint.sh up lab01 | grep '^export ')"
./run_all.sh --campaign-id lab01 --minimum-storage --manage-dns \
  --capacity-family cpx \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" --dr-bucket "$BACKUP_DR_BUCKET"
```

`run_all.sh` deliberately retains successful or partial clusters for evidence
and never guesses that teardown is safe. It prints the exact per-controller
cleanup commands. After evidence is secured, remove those five projects and
run `./scripts/test-dr-endpoint.sh down lab01`; verify the cloud and parent DNS
zone returned to their recorded baseline. `down` retains the independently
delete-protected `<campaign>-dr-data` volume, and a later `up` reattaches it
without reformatting. After recovery evidence expires, explicitly purge it with
`./scripts/test-dr-endpoint.sh purge lab01 "PURGE lab01 DR DATA"`. See
[`docs/TEST_DR_ENDPOINT.md`](docs/TEST_DR_ENDPOINT.md) for the durability and
fail-closed lifecycle contract.

Teardown selects servers, load balancers, firewalls, networks, and volumes by
the exact `project` label, not by a name prefix. This is required when project
names overlap (for example `medium` and `medium-optimized`) and makes parallel
cleanup safe. Legacy placement groups are removed only by the exact
`${project}-spread` name.

If any durable profile-migration state for the project is still
`in_progress`, ordinary `--confirm PROJECT` is intentionally insufficient.
Recover or roll back the migration, or explicitly authorize destruction with
the second phrase printed by `teardown.sh`. Captured volumes are not deleted
until the provider confirms they are detached; a detach timeout leaves the
volume intact and makes teardown fail for operator review.

When `global.domain` is a delegated name below an existing Hetzner zone, set
top-level `hetzner_dns_zone` to the parent, for example
`global.domain: small.lab.example.com` with
`hetzner_dns_zone: example.com`. The role then manages `small.lab`,
`*.small.lab`, and `vpn.small.lab` records in the parent zone.

Set at least `global.domain` and `global.email` in `platform.yaml`. Review every
enabled component and infrastructure size before deployment.

Technology selection is available without hand-editing dotted YAML paths:

```bash
./platform.sh components
./platform.sh enable temporal       # also enables PostgreSQL
./platform.sh enable coroot         # also enables the observability core
./platform.sh enable hipaa          # adds required secrets + observability
./platform.sh enable disaster-recovery # adds native backup + external Velero/Kopia
./platform.sh disable postal        # refuses if an enabled service depends on it
./platform.sh validate              # offline; no Hetzner/Kubernetes mutation
./platform.sh deploy temporal       # targeted, dependency-validated reconcile
```

You can enable a technology later and rerun its targeted deployment or
`deploy all`; Ansible reconciles it idempotently. Disabling changes desired
state but intentionally leaves existing Kubernetes resources running. Explicit
removal is a separate, confirmation-gated command, and PVC-backed components
also require `--delete-data`. This prevents a selector edit from becoming an
accidental data deletion.

`backup` selects the application-native backup jobs. `disaster-recovery`
selects the external whole-cluster layer and automatically enables `backup`
and object storage; configure the independent S3 endpoint and credentials
before deploying it. Disable and remove `disaster-recovery` before disabling
native backup automation. Removing either controller surface retains every
remote backup object. Removing `backup` also reconciles the database operators:
PostgreSQL pgBackRest schedules are removed and MongoDB backup, PITR, and tasks
are disabled, without deleting database data, repositories, PVCs, or existing
backup objects.

For the full platform on the small-resource envelope:

```bash
./platform.sh init medium-optimized
```

Direct Ansible usage is also supported:

```bash
cp inventory.example inventory.yml
$EDITOR inventory.yml
ansible-playbook -i inventory.yml playbooks/deploy_platform.yml
```

Component tags use the same normalized profile contract. ESO, GitLab Runner,
and PMM have independent flags. Metrics, logging, and Grafana remain one
production-tested observability core bundle; PMM, tracing, and Blackbox are
optional dependants of that bundle. PMM is off in `minimal`/`small` to preserve
the constrained node envelope and on in `medium`, `medium-optimized`, and
`production`. Coroot is another optional dependant, installed by
the pinned official operator and sized explicitly for `medium-optimized`.
Alertmanager creates Telegram/email routes only for
enabled channels; its email channel requires the selected Postal service.

ESO does not create a sample application secret by default. Every reconcile
removes the legacy `default/example-secret` ExternalSecret and its generated
Secret while `secrets.eso.example_secret.enabled` is false. For an explicit
demo, first create the source KV-v2 value below Vault's `secret/` mount, then
set its key and property in the profile:

```bash
vault kv put secret/demo/app password='replace-with-a-demo-value'
```

```yaml
secrets:
  eso:
    example_secret:
      enabled: true
      remote_key: demo/app
      remote_property: password
```

Reconciliation fails closed if the source key or property does not exist. The
fixture is for integration demonstrations only; applications should define
their own namespace-scoped ExternalSecrets and Vault paths.

GitLab Gitaly remains a singleton in every bundled profile. Its chart-managed
PDB is pinned to `maxUnavailable: 0` (equivalent to `minAvailable: 1`) so a
voluntary eviction cannot take the only repository-storage pod down. This does
not provide node-failure HA. Before planned maintenance of its node, explicitly
migrate/scale Gitaly or temporarily override the PDB under a reviewed procedure,
then restore the fail-safe value immediately afterward.
The supported `scripts/migrate-profile.sh` workflow performs that exception
automatically only for the node hosting the healthy singleton: it writes a
UID-bound mode-0600 checkpoint, permits one eviction, and restores and verifies
`maxUnavailable: 0` after drain failure or success and again on process exit,
resume, or rollback. Do not patch the PDB manually during that workflow.

Profiles that select GitLab Runner require a GitLab-issued runner
authentication token (the modern `glrt-...` form). Create an instance runner
under **GitLab Admin > CI/CD > Runners**, then provide the token once before
deployment:

```bash
export GITLAB_RUNNER_TOKEN='glrt-...'
```

`platform.sh` and `run_tier.sh` also load it from the gitignored repository
`.env`. The value is retained in `.platform-secrets.yml` under Ansible Vault
encryption for later reconciliations. An enabled Runner with a missing or
legacy registration token fails during secret preflight, before infrastructure
or platform roles change the cluster;
it is never silently omitted. Keep `vault_encrypt_secrets=true`.

For an existing healthy self-hosted GitLab, bootstrap and persist the token
without displaying it:

```bash
scripts/bootstrap-gitlab-runner-token.py \
  --kubeconfig "$KUBECONFIG" \
  --secrets-file playbooks/.platform-secrets.yml \
  --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE"
```

The command is idempotent: it verifies and reuses a live persisted token, or
creates a new instance runner through GitLab's supported API and synchronizes
the result into the ignored `.env` and encrypted secrets file. See
[GitLab Runner token bootstrap](docs/GITLAB_RUNNER_BOOTSTRAP.md) for first-cluster
sequencing, compatibility, and the no-disclosure guarantees.

## Operations

```bash
# Fail-closed local checks
bash scripts/validate-local.sh

# Validate one profile contract without provisioning anything
ansible-playbook playbooks/validate_profile.yml \
  -e @platform-orchestrator/profiles/medium-optimized.yaml

# Health/status
./platform-orchestrator/platform.sh status
./scripts/health-gates.sh

# Disposable-cluster acceptance: component selection plus read/write data paths
./scripts/live-tier-smoke.sh --dry-run
./scripts/live-tier-smoke.sh

# Bounded profile-sized load plus before/after JSON and TSV evidence
./scripts/tier-load-test.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig "$KUBECONFIG" --dry-run
./scripts/tier-load-test.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig "$KUBECONFIG"

# Read-only evidence snapshot without running load
./scripts/collect-live-evidence.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig "$KUBECONFIG" --stage operator-check

# Explicitly remove a disabled, stateless component
./platform-orchestrator/platform.sh remove blackbox --confirm blackbox

# Data-bearing removal requires an additional destructive boundary
./platform-orchestrator/platform.sh remove temporal --confirm temporal --delete-data

# Trigger configured backup CronJobs
./scripts/backup-all.sh --force

# Create and verify a complete encrypted cluster recovery bundle
./platform-orchestrator/platform.sh backup-cluster \
  --vault-init-file /secure/state/.vault-init-k8s.json \
  --recipient age1... --force
./platform-orchestrator/platform.sh restore-cluster \
  --archive /secure/k8s-cluster-....tar.gz.age --mode verify \
  --identity /secure/age-identity.txt
./platform-orchestrator/platform.sh restore-cluster \
  --archive /secure/k8s-cluster-....tar.gz.age --mode operator-state \
  --identity /secure/age-identity.txt --output-dir /secure/recovery/k8s
# After checking out repository.bundle at repository/git-revision.txt and
# applying worktree.patch, restore only validated, non-colliding untracked files.
./scripts/restore-repository-untracked.sh \
  /secure/recovery/k8s/repository /path/to/checkout

# Capture rollback baseline and inspect an upgrade
./scripts/upgrade-platform.sh snapshot
./scripts/upgrade-platform.sh plan

# Dry-run restore drills before executing against a test cluster
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
./scripts/pg-restore-drill.sh --dry-run
./scripts/restore-drill.sh --component postgresql --backup PGBACKREST_SET --dry-run
./scripts/vault-restore-drill.sh --dry-run
./scripts/restore-drill.sh --component mongodb --backup BACKUP_CR --dry-run
./scripts/restore-drill.sh --component seaweedfs --backup VELERO_BACKUP --dry-run

# Plan any named-profile transition; no cluster mutation
export BACKUP_DR_ENDPOINT=https://s3.example-provider.com
export BACKUP_DR_BUCKET=company-platform-dr
./platform-orchestrator/platform.sh migrate --target production plan

# Destructive: exact confirmation is required
./platform-orchestrator/platform.sh destroy
```

When Vault is selected, `backup-cluster` requires the exact
Ansible-Vault-encrypted initialization file. The schema-v2 recovery bundle
keeps that file encrypted, verifies its structure without logging contents,
and records it as a required Vault recovery dependency. Store the Ansible Vault
password separately from the bundle decryption identity.

Schema-v2 completion receipts bind the archive to the project, source cluster
UID, and exact Velero prefix. Before destructive replacement, pass that receipt
to `teardown.sh --require-backup-receipt`; teardown re-downloads and hashes the
remote receipt, checksum, and archive before its first provider operation. Use
the recovered exact config/secrets/repository state with the `velero-bootstrap`
tag, then run strict Velero restore and the structured native backup catalog.
See [Backup and restore](BACKUP_RESTORE.md) for the ordered recovery commands.
Application consistency is a separate destructive gate: new backups bind the
schema-v2 native catalog hash into that receipt, and `scripts/native-restore.sh`
replays exact native artifacts in dependency order with replacement-UID-bound,
resumable checkpoints. A successful Velero restore alone is not a completed
production recovery.

The platform CLI also loads the DR values from the mode-`0600`, gitignored
`.env`. Blank named-profile endpoint/bucket fields fall back to that
environment; DR access and secret keys stay environment-only and are never
placed on the Ansible command line.

No command in the restore, upgrade, rollback, or teardown path should be run
against production without a recorded maintenance window and verified backup.
`live-tier-smoke.sh` writes uniquely named temporary S3, PostgreSQL, and Vault
sentinels, verifies metrics/logging/GitOps HTTP paths and every selected
Gateway API TLS route from inside the private cluster, then removes its test
data. Run its mutating mode only against an
explicitly authorized disposable or maintenance-window cluster.
`tier-load-test.sh` scales bounded HTTP, S3, PostgreSQL, Vault, and Dragonfly
work to the selected named profile, skips disabled technologies, and fails
closed on operation errors, phase timeouts, node pressure, unhealthy workloads,
or excessive restart growth. It uses version-pinned clients, removes every
test key/object/table/path, and writes `summary.json`, `phases.tsv`, logs, and
secret-free live snapshots below the selected output directory. Vault load
requires the encrypted profile init file and `ANSIBLE_VAULT_PASSWORD_FILE`.

## Documentation

- [Deployment guide](DEPLOYMENT.md)
- [Technology catalog and profile matrix](docs/TECHNOLOGY_CATALOG.md)
- [Operations runbook](RUNBOOK.md)
- [Backup and restore](BACKUP_RESTORE.md)
- [Security hardening](SECURITY_HARDENING.md)
- [HIPAA-oriented hardening scope](HIPAA_COMPLIANCE.md)
- [Observability stack](OBSERVABILITY.md)
- [Upgrade runbook](UPGRADE_RUNBOOK.md)
- [GitLab 18.11 to 19.1 plan](docs/GITLAB_UPGRADE_PLAN.md)
- [Validation and CI](docs/CI_AUTOMATION.md)
- [Five-tier live test report (2026-07-21)](docs/FIVE_TIER_LIVE_TEST_2026-07-21.md)

## Validation scope

The CI and local suite run YAML/Ansible linting, ShellCheck, playbook syntax,
version-contract checks, and pytest unit/static component-contract tests. They
do not claim to be a live Hetzner/Kubernetes end-to-end deployment. Live
restore and upgrade drills require an explicitly authorized test cluster.

## License

MIT. See [LICENSE](LICENSE).
