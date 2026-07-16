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
export ANSIBLE_VAULT_PASSWORD_FILE="$HOME/.vault_pass"
export HCLOUD_TOKEN="replace-me"
```

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

GitLab chart 10 cannot be enabled without PostgreSQL, Dragonfly, and object
storage. The normalizer rejects that invalid profile before infrastructure is
changed.

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
./platform.sh deploy cluster
./platform.sh deploy secrets
./platform.sh deploy object-storage
./platform.sh deploy databases
./platform.sh deploy gitlab
./platform.sh deploy gitops
./platform.sh deploy observability
```

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
```

GitLab uses the chart-generated `gitlab-toolbox-backup` CronJob and a separate
`gitlab-rails-secrets-backup` job. Both are required for recovery.

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
