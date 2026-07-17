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

There are four runtime tiers. `medium-optimized` is intentionally a named
profile rather than a fifth tier: it sets `tier: medium` to retain every medium
service and `resource_tier: small` to select the compact resource envelope.
Do not rename it to `medium_optimized` or change its tier to `small`; both would
break the explicit profile contract and are rejected before provisioning.

For the budget production-oriented deployment:

```bash
./platform.sh init medium-optimized
```

This profile uses three schedulable `cx23` control planes, four `cx33` workers,
a `cx23` bastion, and `lb11`. Stateful quorum/data services remain replicated;
stateless services default to one replica with bounded autoscaling. Choose the
`production` profile when stateless workload continuity during node maintenance
is required. Production backups must also be copied to storage outside this
cluster.

GitLab chart 10 cannot be enabled without PostgreSQL, Dragonfly, and object
storage. The normalizer rejects that invalid profile before infrastructure is
changed.

Inspect and change the selected technologies through the orchestrator:

```bash
./platform.sh components
./platform.sh enable gitlab       # enables storage, PostgreSQL, and Dragonfly
./platform.sh disable daytona
./platform.sh validate
```

`enable` adds required dependencies. `disable` refuses when another enabled
technology still depends on the target. The validated dependency graph also
covers ESO -> secrets, database engines -> databases, Runner -> GitLab,
GlitchTip -> PostgreSQL + Dragonfly, APM -> Elasticsearch, Temporal ->
PostgreSQL + Elasticsearch, Postal -> Dragonfly, tracing -> observability +
storage, Blackbox -> observability, and backup -> storage. Metrics, logging,
Grafana, and PMM are intentionally deployed as one observability core bundle.
Coroot -> observability is also enforced; HIPAA-oriented hardening requires
secrets, observability, Cilium encryption, and active log redaction. The full
selector, profile matrix, and removal classes are in the
[technology catalog](docs/TECHNOLOGY_CATALOG.md).
Alert delivery channels remain settings under `alerting.telegram.enabled` and
`alerting.email.enabled`; email requires Postal, while Telegram also requires
`ALERT_TELEGRAM_BOT_TOKEN` and `ALERT_TELEGRAM_CHAT_ID` at deployment time.

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
balancer policies converge on rerun. Server removal/type change is blocked
unless `hetzner_allow_destructive_reconcile=true` is passed after affected
nodes have been drained.

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
./platform.sh deploy coroot
./platform.sh deploy tracing
./platform.sh deploy autoscaling
./platform.sh deploy temporal
./platform.sh deploy postal
./platform.sh deploy backup
./platform.sh deploy glitchtip
./platform.sh deploy apm
./platform.sh deploy blackbox
./platform.sh deploy daytona
./platform.sh deploy hipaa
```

Every targeted deployment always runs profile normalization and dependency
validation first. It fails if the component is disabled; enable it explicitly
instead of overriding an Ansible variable on the command line.

`deploy coroot` reconciles the observability bundle plus the pinned official
Coroot operator/CE resources. Its eBPF node agent requires privileged Pod
Security admission, scoped only to the `coroot` namespace. `deploy hipaa`
reconciles network host controls and every selected log collector so redaction
is active rather than an unused configuration object.

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
you perform the documented restore procedure.

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
before provisioning Hetzner resources. After deployment, create an encrypted full-cluster bundle with
`platform.sh backup-cluster`; see [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

Do not replace one named profile with another and run `deploy all`. Use
`platform.sh migrate --target PROFILE plan|execute`, followed by
`resume|status|rollback|finalize`. All 20 distinct transitions among `minimal`,
`small`, `medium`, `medium-optimized`, and `production` use the same external
backup gate. The workflow expands to the larger source/target topology first,
resizes retained nodes one at a time, checks etcd around every control-plane
change, and keeps scale-in and source-data deletion behind `finalize`.

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

Type the exact confirmation when prompted. The script deletes and verifies
project-prefixed load balancers, servers, volumes, firewalls, placement groups,
SSH keys, subnets, and the network. It intentionally preserves DNS and the
global kubeconfig.
