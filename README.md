# Ansible Kubernetes Platform for Hetzner Cloud

This repository provisions a Kubernetes platform on Hetzner Cloud and installs
the selected platform services. It is infrastructure automation; it does not
ship or deploy an application repository by default.

## What it manages

- Hetzner network, firewalls, bastion, control-plane and worker servers, load
  balancer, and platform DNS records.
- Kubernetes v1.35.6 with Cilium, Gateway API, cert-manager, Hetzner CCM/CSI,
  and a private-node topology.
- Optional profile-controlled services: Vault and External Secrets Operator,
  SeaweedFS, PostgreSQL, MongoDB, Dragonfly, GitLab, Argo CD, observability,
  KEDA, Temporal, Postal, GlitchTip, APM, Blackbox Exporter, and Daytona.
- Scheduled backups, verification jobs, restore-drill scripts, staged upgrades,
  exact Helm rollback baselines, and verified teardown.

The runtime has four capability tiers and five named profiles:

| Profile | Capability tier | Resource tier | Default topology | Service scope |
|---|---|---|---|---|
| `minimal` | minimal | minimal | 1 schedulable control plane + 1 worker | Core development platform; no GitLab or medium-only services |
| `small` | small | small | 1 control plane + 2 workers | Compact GitLab platform with PostgreSQL, Dragonfly, storage, secrets, GitOps, and monitoring |
| `medium` | medium | medium | 3 control planes + 2 workers | Full platform with standard medium sizing |
| `medium-optimized` | medium | small | 3 schedulable control planes + 4 workers | Full medium service set with conservative requests, replicas, retention, and autoscaling |
| `production` | production | production | 3 control planes + 3 workers | Higher stateless workload redundancy and the largest retention/storage defaults |

`tier` controls which capabilities are installed. `resource_tier` controls
default pod requests, limits, and stateless replica counts. This separation is
what lets `medium-optimized` retain the full medium toolset without silently
allocating the normal-medium footprint. GitLab chart 10 requires PostgreSQL,
Dragonfly, and object storage; profile validation rejects an invalid
combination.

The optimized profile keeps three-way control-plane, Vault, PostgreSQL,
MongoDB, SeaweedFS, and Elasticsearch-master topology. Recoverable stateless
services run one replica by default and autoscaling is capped at four. It is a
production-oriented budget profile, but it does not provide the same workload
availability during maintenance as the `production` profile. Store production
backups outside the cluster for disaster recovery.

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

Create the local secret-encryption password file and export required values:

```bash
umask 077
openssl rand -base64 48 > ~/.vault_pass
export ANSIBLE_VAULT_PASSWORD_FILE="$HOME/.vault_pass"
export HCLOUD_TOKEN="replace-me"
```

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

Set at least `global.domain` and `global.email` in `platform.yaml`. Review every
enabled component and infrastructure size before deployment.

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

Component tags use the same normalized profile contract; disabling a component
in `platform.yaml` is respected by full and tagged runs.

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

# Trigger configured backup CronJobs
./scripts/backup-all.sh --force

# Capture rollback baseline and inspect an upgrade
./scripts/upgrade-platform.sh snapshot
./scripts/upgrade-platform.sh plan

# Dry-run restore drills before executing against a test cluster
./scripts/gitlab-restore-test.sh --dry-run --restore --backup BACKUP_ID
./scripts/pg-restore-drill.sh --dry-run
./scripts/vault-restore-drill.sh --dry-run

# Destructive: exact confirmation is required
./platform-orchestrator/platform.sh destroy
```

No command in the restore, upgrade, rollback, or teardown path should be run
against production without a recorded maintenance window and verified backup.

## Documentation

- [Deployment guide](DEPLOYMENT.md)
- [Operations runbook](RUNBOOK.md)
- [Backup and restore](BACKUP_RESTORE.md)
- [Security hardening](SECURITY_HARDENING.md)
- [Upgrade runbook](UPGRADE_RUNBOOK.md)
- [GitLab 18.11 to 19.1 plan](docs/GITLAB_UPGRADE_PLAN.md)
- [Validation and CI](docs/CI_AUTOMATION.md)

## Validation scope

The CI and local suite run YAML/Ansible linting, ShellCheck, playbook syntax,
version-contract checks, and pytest unit/static component-contract tests. They
do not claim to be a live Hetzner/Kubernetes end-to-end deployment. Live
restore and upgrade drills require an explicitly authorized test cluster.

## License

MIT. See [LICENSE](LICENSE).
