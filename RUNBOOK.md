# Deployment Runbook — ansible-k8s-full-setup

> Production-grade operational guide for deploying the full platform from scratch
> on Hetzner Cloud to a production-ready state with the nest-react-boilerplate
> application deployed via ArgoCD GitOps.

| Item | Detail |
|------|--------|
| **Total estimated time** | 3–5 hours (first run, medium tier) |
| **Target platform** | Hetzner Cloud + Kubernetes (Kubespray) + Cilium |
| **Services** | ArgoCD, GitLab CE, PostgreSQL, SeaweedFS S3, Vault, ESO, VictoriaMetrics, Loki, Grafana, KEDA, Dragonfly, Temporal, cert-manager, Elasticsearch, Postal |
| **Application** | nest-react-boilerplate (deployed via ArgoCD from `nest-react-boilerplate/deploy/k8s/`) |

---

## Table of Contents

1. [Prerequisites](#1-prerequisites)
2. [Phase 1 — Initial Setup (one-time)](#2-phase-1--initial-setup-one-time)
3. [Phase 2 — Platform Deployment](#3-phase-2--platform-deployment)
4. [Phase 3 — Application Deployment (nest-react-boilerplate)](#4-phase-3--application-deployment-nest-react-boilerplate)
5. [Phase 4 — Verification](#5-phase-4--verification)
6. [Credential Retrieval](#6-credential-retrieval)
7. [Routine Operations](#7-routine-operations)
8. [Troubleshooting](#8-troubleshooting)
9. [Rollback](#9-rollback)
10. [Teardown](#10-teardown)
11. [Appendix — Tier Comparison](#11-appendix--tier-comparison)

---

## 1. Prerequisites

### 1.1 Hetzner Cloud Account

- Active account at <https://console.hetzner.cloud>
- API token with **read/write** permissions
  1. Console → **Security** → **API Tokens** → **Generate Token**
  2. Scope: **Read & Write**
  3. Save the token — it is shown only once

### 1.2 SSH Key Pair

- Ed25519 key pair at `~/.ssh/id_ed25519` (public key uploaded to Hetzner console)
  ```bash
  # Generate if missing
  ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N "" -C "k8s-platform"

  # Upload public key to Hetzner
  # Console → Security → SSH Keys → New SSH Key
  # Paste contents of ~/.ssh/id_ed25519.pub
  ```

### 1.3 Domain Name with DNS Access

- A registered domain (e.g., `example.com`) where you can create `A` records
- The platform creates DNS records for: `gitlab`, `argocd`, `grafana`, `vault`,
  `s3`, `object-storage`, `temporal`, `kibana`, `mail`, `registry`, `kas`, `vpn`,
  `api`, `app`, and `*.daytona` (if Daytona is enabled)

### 1.4 Local Tooling

| Tool | Minimum Version | Install |
|------|----------------|---------|
| Ansible Core | 2.16.0 | `pip install ansible-core>=2.16.0` |
| kubectl | 1.30 | <https://kubernetes.io/docs/tasks/tools/> |
| Helm | 3.14 | <https://helm.sh/docs/intro/install/> |
| hcloud CLI | latest | `brew install hcloud` / `snap install hcloud` |
| yq | 4.0 | `brew install yq` (for platform orchestrator) |
| Node.js | 24.x | `nvm install 24` (for nest-react-boilerplate build) |

```bash
# One-shot install (Linux / Debian-based)
sudo apt update && sudo apt install -y ansible git python3-pip
pip3 install jinja2 netaddr

# Install Ansible collections required by the platform
cd /tmp/ansible-k8s-full-setup
ansible-galaxy collection install -r requirements.yml

# Verify
ansible --version          # ≥ 2.16.0
kubectl version --client   # ≥ 1.30
helm version               # ≥ 3.14
hcloud version             # latest
yq --version               # ≥ 4.0
```

### 1.5 Clone the Repository

```bash
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup
```

### 1.6 Environment Variables

```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
```

> **Note:** You can also place this in `~/.env` and source it:
> ```bash
> echo 'HCLOUD_TOKEN=your-token' >> ~/.env
> set -a; source ~/.env; set +a
> ```

---

## 2. Phase 1 — Initial Setup (one-time)

**Estimated time:** 15 minutes

### 2.1 Choose Deployment Method

The platform supports two deployment methods. **Choose one.**

| Method | Best for | Entry point |
|--------|----------|-------------|
| **Platform Orchestrator** (recommended) | Full lifecycle management, profiles, credentials | `platform-orchestrator/platform.sh` |
| **Ansible Directly** | Simple, single-command deploy | `playbooks/deploy_platform.yml` |

---

### 2.1A Platform Orchestrator (Recommended)

```bash
cd platform-orchestrator

# Initialize configuration from a tier profile
./platform.sh init medium
# Available profiles: example, minimal, small, medium, production, medium-optimized

# Edit the generated platform.yaml
vim platform.yaml
```

Set these required fields in `platform.yaml`:

```yaml
global:
  domain: "example.com"        # REQUIRED — change to your domain
  email: "admin@example.com"   # REQUIRED — change to your email
  project: k8s                 # optional: prefix for Hetzner resources
```

Verify:

```bash
cat platform.yaml | grep -E 'domain:|email:|tier:'
```

**Expected output:**
```
tier: medium
  domain: "example.com"
  email: "admin@example.com"
```

---

### 2.1B Ansible Directly

```bash
# Copy the example inventory
cp inventory.example inventory.yml

# Edit it
vim inventory.yml
```

Required changes in `inventory.yml`:

```yaml
all:
  vars:
    domain: example.com          # REQUIRED
    email: admin@example.com     # REQUIRED
    project_name: k8s
    tier: medium                 # minimal | small | medium | production
    hetzner_region: hel1
    ssh_key_path: ~/.ssh/id_ed25519
```

---

### 2.2 Verify Prerequisites

```bash
# Check hcloud CLI can authenticate
hcloud server list
# Expected: empty list or existing servers (exit code 0)

# Check Ansible collections
ansible-galaxy collection list kubernetes.core
# Expected: kubernetes.core  <version>

# Check SSH key exists
ls -la ~/.ssh/id_ed25519
# Expected: -rw-------  1 user group ... id_ed25519

# Check environment
echo $HCLOUD_TOKEN
# Expected: your token (non-empty)
```

---

## 3. Phase 2 — Platform Deployment

**Estimated time:**
| Tier | Nodes | Estimated time |
|------|-------|----------------|
| minimal | 2 | 45–60 minutes |
| small | 3 | 50–70 minutes |
| medium | 5 | 90–150 minutes |
| production | 6+ | 120–300 minutes |

### 3.1 Run the Full Deployment

#### Option A: Platform Orchestrator

```bash
cd platform-orchestrator

# Full deployment (all roles, sequentially)
./platform.sh deploy all
```

This runs `ansible-playbook playbooks/deploy_platform.yml` with the
configuration from `platform.yaml`. All roles execute in this order:

| # | Role | Tag | Approx. time |
|---|------|-----|-------------|
| 1 | generate-secrets | `always` | <1 min |
| 2 | hetzner-infra | `infrastructure` | 15–30 min |
| 3 | network-security | `network` | 5–10 min |
| 4 | k8s-cluster-management | `cluster` | 30–60 min |
| 5 | k8s-secrets | `secrets` | 10–15 min |
| 6 | object-storage | `storage` | 5–10 min |
| 7 | k8s-observability | `observability` | 10–20 min |
| 8 | elasticsearch | `elasticsearch` | 5–10 min |
| 9 | k8s-databases | `databases` | 10–15 min |
| 10 | gitlab-selfhosted | `gitlab` | 15–30 min |
| 11 | k8s-gitops | `gitops` | 5–10 min |
| 12 | k8s-autoscaling | `autoscaling` | 2–5 min |
| 13 | dragonfly | `dragonfly` | 2–5 min |
| 14 | temporal | `temporal` | 3–5 min |
| 15 | postal | `postal` | 3–5 min |
| 16 | blackbox-exporter | `blackbox` | 1–2 min |
| — | hipaa-hardening | `hipaa` | 1–2 min |

#### Option B: Ansible Directly

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com \
  -e email=admin@example.com \
  -e tier=medium \
  -v
```

### 3.2 Monitor Progress

Watch the Ansible output in real-time. Key milestones to watch for:

```
PLAY [Deploy Kubernetes Platform] *************************************************

TASK [generate-secrets : Generate platform credentials] **************************
ok: [localhost]

TASK [hetzner-infra : Provision Hetzner Cloud Infrastructure] ********************
# This creates servers, network, firewall, LB — takes the longest

TASK [k8s-cluster-management : Deploy Kubernetes Cluster] ****************************
# Kubespray installation — watch for node joining

TASK [gitlab-selfhosted : Install GitLab with Helm] ******************************
# GitLab takes time to become ready; the role does NOT wait

TASK [k8s-gitops : Install ArgoCD with Helm] ***********************************
ok: [localhost]

TASK [Deploy Kubernetes Platform : Display deployment completion summary] *********
```

**Expected final output:**
```
==========================================
  Platform Deployment Complete
  Tier: medium
  Project: k8s
  Domain: example.com
==========================================
Services deployed:
  - Kubernetes Cluster with Cilium CNI
  - GitLab CE: https://gitlab.example.com
  - ArgoCD: https://argocd.example.com
  - Grafana: https://grafana.example.com
  - ...
==========================================
```

### 3.3 Deploying Individual Components (Recovery / Partial)

If deployment fails at a specific role, you can re-run just that component:

```bash
# Platform Orchestrator
./platform.sh deploy infrastructure
./platform.sh deploy cluster
./platform.sh deploy secrets
./platform.sh deploy storage
./platform.sh deploy databases
./platform.sh deploy gitlab
./platform.sh deploy gitops
./platform.sh deploy observability
./platform.sh deploy autoscaling

# Or via Ansible tags
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml -e domain=example.com -e email=admin@example.com \
  --tags gitlab
```

Available component names: `infra`, `network`, `dns`, `cluster`, `tls`, `object-storage`,
`secrets`, `databases`, `gitlab`, `gitops`, `observability`, `autoscaling`, `glitchtip`,
`apm`, `blackbox`, `daytona`, `all`.

---

## 4. Phase 3 — Application Deployment (nest-react-boilerplate)

**Estimated time:** 10–20 minutes

The nest-react-boilerplate application is deployed via ArgoCD GitOps.
ArgoCD watches the manifests in `nest-react-boilerplate/deploy/k8s/` and
automatically syncs them to the cluster.

### 4.1 Configure Kubernetes Secrets

Before deploying the ArgoCD Application, create the required secrets in the cluster.

#### 4.1.1 Container Registry Credentials

The application images are hosted on GHCR. Create a pull secret:

```bash
# Create the ghcr-credentials secret (Docker registry auth)
kubectl create secret docker-registry ghcr-credentials \
  --namespace=production \
  --docker-server=ghcr.io \
  --docker-username=<GH_USERNAME> \
  --docker-password=<GH_PERSONAL_ACCESS_TOKEN> \
  --docker-email=<email>

# Verify
kubectl get secret ghcr-credentials -n production -o yaml
```

**Expected output:**
```
apiVersion: v1
kind: Secret
metadata:
  name: ghcr-credentials
  namespace: production
type: kubernetes.io/dockerconfigjson
```

> **Note:** The GitHub Personal Access Token must have the `read:packages` scope.

#### 4.1.2 Application Secrets

Create the `nest-react-boilerplate-production-secrets` secret with all
application-specific environment variables:

```bash
kubectl create secret generic nest-react-boilerplate-production-secrets \
  --namespace=production \
  --from-literal=DB_HOST="<postgres-host>.databases.svc.cluster.local" \
  --from-literal=DB_PORT=5432 \
  --from-literal=DB_NAME="<database_name>" \
  --from-literal=DB_USER="<username>" \
  --from-literal=DB_PASSWORD="<password>" \
  --from-literal=REDIS_HOST=dragonfly.dragonfly.svc.cluster.local \
  --from-literal=REDIS_PORT=6379 \
  --from-literal=REDIS_PASSWORD="<dragonfly_password>" \
  # Dragonfly provides Redis v6-compatible interface
  --from-literal=JWT_SECRET="<jwt_secret_value>" \
  --from-literal=JWT_REFRESH_SECRET="<jwt_refresh_secret_value>" \
  --from-literal=APP_BASE_URL="https://app.example.com" \
  --from-literal=FRONTEND_URL="https://example.com"
```

Retrieve the database and Dragonfly credentials from the platform:

```bash
# PostgreSQL password
kubectl get secret <project_name>-pg-pguser-app -n databases \
  -o jsonpath='{.data.password}' | base64 -d

# Dragonfly password
kubectl get secret dragonfly-auth -n dragonfly \
  -o jsonpath='{.data.password}' | base64 -d

# JWT secrets (from platform secrets file)
cat playbooks/.platform-secrets.yml | grep -E 'jwt_secret|jwt_refresh'
```

### 4.2 Configure Ingress Hosts and CORS

Edit `nest-react-boilerplate/deploy/k8s/values.yaml` to set the correct
ingress hostnames and CORS origins:

```yaml
# In ingress section, set hosts to your domain
ingress:
  enabled: true
  hosts:
    - host: app.example.com
      paths:
        - path: /
          pathType: Prefix
    - host: api.example.com
      paths:
        - path: /
          pathType: Prefix

# In CORS section, set allowed origins
cors:
  origins:
    - https://example.com
    - https://app.example.com
    - https://admin.example.com
```

### 4.3 Apply the ArgoCD Application

```bash
# The ArgoCD Application manifest is at nest-react-boilerplate/deploy/k8s/
kubectl apply -f nest-react-boilerplate/deploy/k8s/argocd-application.yaml
```

Or, if using a Helm chart directly:

```bash
kubectl apply -f nest-react-boilerplate/deploy/k8s/
```

### 4.4 Verify ArgoCD Sync

```bash
# Check the ArgoCD Application status
kubectl get application nest-react-boilerplate -n argocd -o wide

# Expected output:
# NAME                        SYNC STATUS   HEALTH STATUS
# nest-react-boilerplate      Synced        Healthy

# Or via ArgoCD CLI (if installed)
argocd app get nest-react-boilerplate
```

If the application is out of sync, trigger a manual sync:

```bash
kubectl patch application nest-react-boilerplate -n argocd \
  --type merge -p '{"spec": {"syncPolicy": {"automated": {"selfHeal": true, "prune": true}}}}'
```

### 4.5 Verify Application Pods

```bash
kubectl get pods -n production
# Expected: all pods in Running state

kubectl get svc -n production
# Expected: services created for frontend and backend

kubectl logs -n production -l app=nest-react-boilerplate --tail=50
# Expected: application startup logs, no errors
```

---

## 5. Phase 4 — Verification

### 5.1 Infrastructure Checks

```bash
# Hetzner Cloud resources
hcloud server list
hcloud network list
hcloud load-balancer list
hcloud firewall list

# Expected: servers named k8s-bastion, k8s-cp-1..3, k8s-worker-1..3;
#   network k8s-network; LB k8s-lb; firewalls fw-bastion, fw-nodes
```

### 5.2 Kubernetes Cluster Health

```bash
# Node status — all Ready
kubectl get nodes
# Expected:
# NAME             STATUS   ROLES           AGE   VERSION
# k8s-cp-1         Ready    control-plane   45m   v1.35.4
# k8s-cp-2         Ready    control-plane   45m   v1.35.4
# k8s-cp-3         Ready    control-plane   45m   v1.35.4
# k8s-worker-1     Ready    <none>          44m   v1.35.4
# k8s-worker-2     Ready    <none>          44m   v1.35.4

# Pods — no non-Running / non-Completed
kubectl get pods -A | grep -vE 'Running|Completed'
# Expected: empty output (or only init/ephemeral containers)

# Storage classes
kubectl get sc
# Expected: hcloud-volumes (default)

# Cilium
kubectl get pods -n kube-system | grep cilium
# Expected: one cilium pod per node, all Running

# cert-manager
kubectl get pods -n cert-manager
# Expected: cert-manager and cert-manager-webhook Running
```

### 5.3 Service Health Checks

```bash
# GitLab
kubectl get pods -n gitlab | head -20
# Expected: all pods Running/Completed

# ArgoCD
kubectl get pods -n argocd
# Expected: argocd-server, argocd-repo-server, argocd-application-controller Running

# Grafana
kubectl get pods -n monitoring | grep grafana
# Expected: grafana pod Running

# VictoriaMetrics
kubectl get pods -n monitoring | grep victoria
# Expected: victoria-metrics-server, victoria-metrics-single Running

# Loki
kubectl get pods -n monitoring | grep loki
# Expected: loki Running

# Vault
kubectl get pods -n vault
# Expected: vault-0 (and vault-1, vault-2 on medium/production) Running

# Vault unseal status
kubectl exec -n vault vault-0 -- vault status
# Expected:
# Key             Value
# ---             -----
# Seal Type       shamir
# Initialized     true
# Sealed          false
# Total Shares    3
# Threshold       2
# Version         1.21

# PostgreSQL
kubectl get pods -n databases | grep postgres
# Expected: postgres pods Running, pgbouncer Running

# SeaweedFS
kubectl get pods -n storage
# Expected: seaweedfs-volume, seaweedfs-filer, seaweedfs-master Running

# KEDA
kubectl get pods -n keda
# Expected: keda-operator Running

# Dragonfly
kubectl get pods -n dragonfly
# Expected: dragonfly pods Running

# Temporal
kubectl get pods -n temporal
# Expected: temporal pods Running

# Elasticsearch
kubectl get pods -n elasticsearch
# Expected: elasticsearch-data, elasticsearch-master Running

# Kibana
kubectl get pods -n elasticsearch | grep kibana
# Expected: kibana Running
```

### 5.4 URL Verification Table

All URLs are accessible via the Hetzner Load Balancer IP. Verify DNS records
point the subdomains to the LB IP.

| Service | URL | Access | Expected |
|---------|-----|--------|----------|
| **GitLab CE** | `https://gitlab.<domain>` | VPN required | Login page, version 18.11.3 |
| **GitLab Registry** | `https://registry.<domain>` | Public (Docker push/pull) | Registry service responding |
| **ArgoCD** | `https://argocd.<domain>` | VPN required | ArgoCD login page, v3.4.2 |
| **Grafana** | `https://grafana.<domain>` | VPN required | Grafana login, 12+ dashboards |
| **Vault** | `https://vault.<domain>` | VPN required | Vault UI, unsealed |
| **SeaweedFS Console** | `https://seaweedfs.<domain>` | Public | Object storage web console |
| **SeaweedFS S3** | `https://s3.<domain>` | Public (S3 API) | S3-compatible API endpoint |
| **Temporal** | `https://temporal.<domain>` | VPN required | Temporal Web UI |
| **Kibana** | `https://kibana.<domain>` | VPN required | Kibana dashboard |
| **Postal** | `https://mail.<domain>` | VPN required | Postal admin UI |
| **Headscale VPN** | `wss://vpn.<domain>` | Public (WireGuard over WS) | WireGuard connection |
| **App (nest-react-boilerplate)** | `https://app.<domain>` | Public | Application frontend |
| **API (nest-react-boilerplate)** | `https://api.<domain>` | Public | API health endpoint |

Verify with curl:

```bash
# Public services
curl -sI https://s3.example.com | head -5
curl -sI https://app.example.com | head -5
curl -sI https://api.example.com | head -5

# VPN-only services (requires Headscale connection first)
curl -sI https://gitlab.example.com | head -5
curl -sI https://argocd.example.com | head -5
curl -sI https://grafana.example.com | head -5
curl -sI https://vault.example.com | head -5
```

### 5.5 DNS Record Verification

```bash
# Check DNS propagation
nslookup gitlab.example.com
nslookup argocd.example.com
nslookup s3.example.com
nslookup vault.example.com

# Expected: all return the Hetzner Load Balancer public IP
```

---

## 6. Credential Retrieval

All platform credentials are generated during deployment and persisted to
`playbooks/.platform-secrets.yml` (never committed to Git).

### 6.1 Platform Orchestrator

```bash
cd platform-orchestrator
./platform.sh credentials
```

### 6.2 Manual Retrieval

```bash
# GitLab root password
kubectl get secret gitlab-gitlab-initial-root-password -n gitlab \
  -o jsonpath='{.data.password}' | base64 -d
echo

# ArgoCD admin password
kubectl get secret argocd-initial-admin-secret -n argocd \
  -o jsonpath='{.data.password}' | base64 -d
echo

# Grafana admin password
kubectl get secret grafana -n monitoring \
  -o jsonpath='{.data.admin-password}' | base64 -d
echo

# PostgreSQL application password
kubectl get secret <project_name>-pg-pguser-app -n databases \
  -o jsonpath='{.data.password}' | base64 -d
echo

# Dragonfly password
kubectl get secret dragonfly-auth -n dragonfly \
  -o jsonpath='{.data.password}' | base64 -d
echo

# Object storage (SeaweedFS S3) credentials
cat playbooks/.platform-secrets.yml | grep object_storage
# object_storage_access_key: <access_key>
# object_storage_secret_key: <secret_key>

# Vault root token
kubectl exec -n vault vault-0 -- vault operator unseal -field key 2>/dev/null
# Or from the generate-secrets output during deployment

# Headscale API key
grep headscale_api_key playbooks/.platform-secrets.yml

# JWT secrets (for application)
grep -E 'jwt_secret|jwt_refresh' playbooks/.platform-secrets.yml

# All generated secrets
cat playbooks/.platform-secrets.yml
```

### 6.3 Important

> **Back up `playbooks/.platform-secrets.yml` immediately after deployment.**
> This file contains all passwords and keys. Without it, you cannot recover
> service passwords. Store it in a password manager or encrypted vault.

```bash
# Copy to a safe location
cp playbooks/.platform-secrets.yml /secure/backups/platform-secrets-$(date +%F).yml
chmod 600 /secure/backups/platform-secrets-$(date +%F).yml
```

---

## 7. Routine Operations

### 7.1 Connect to the Cluster

```bash
# kubectl config is automatically configured by the playbook
kubectl config get-contexts
kubectl config current-context

# If kubectl config is not set, copy from bastion
scp -i ~/.ssh/id_ed25519 root@<bastion-ip>:.kube/config ~/.kube/config
```

### 7.2 Access the Bastion

```bash
ssh -i ~/.ssh/id_ed25519 root@<bastion-ip>
```

### 7.3 Connect via Headscale VPN

```bash
# Get WireGuard config from Headscale
# 1. Log in to ArgoCD at https://argocd.<domain>
# 2. Or generate via Headscale API:
curl -s https://vpn.<domain> -H "X-Api-Key: <headscale_api_key>"

# Alternative: download WireGuard profile through VPN UI
```

### 7.4 View Logs

```bash
# Kubernetes pod logs
kubectl logs -n production -l app=nest-react-boilerplate --tail=100

# Loki (log aggregation)
# Access via Grafana at https://grafana.<domain> → Explore → Loki

# Elasticsearch logs
# Access via Kibana at https://kibana.<domain>
```

### 7.5 Backup Status

```bash
# GitLab backup CronJob
kubectl get cronjob -n gitlab | grep backup

# Check last backup
kubectl get jobs -n gitlab | grep backup

# pgBackRest (PostgreSQL)
kubectl exec -n databases -it \
  $(kubectl get pods -n databases -l postgres-operator.crunchydata.com/instance-set=ha -o name | head -1) \
  -- pgbackrest info

# Manual GitLab backup
kubectl exec -n gitlab -it \
  $(kubectl get pods -n gitlab -l app=toolbox -o name) \
  -- backup-utility
```

### 7.6 Monitoring Dashboards

```bash
# Grafana dashboards are auto-provisioned
# Access: https://grafana.<domain>
# Login: admin / <grafana_admin_password from .platform-secrets.yml>

# Available dashboards:
# - Kubernetes Cluster Monitoring
# - Cilium Network Policy
# - Pod / Node / Namespace resource usage
# - Vault Operations
# - PostgreSQL Metrics (PMM)
# - SeaweedFS Storage
# - Application (nest-react-boilerplate) metrics
# - KEDA ScaledObject events
# - Dragonfly Cache Stats
# - Temporal Workflow Metrics
# - Alertmanager Alerts
```

---

## 8. Troubleshooting

### 8.1 Deployment Fails at a Specific Role

```bash
# Re-run only the failed role
./platform.sh deploy <component>

# Components: infra, network, dns, cluster, tls, object-storage,
#   secrets, databases, gitlab, gitops, observability, autoscaling,
#   glitchtip, apm, blackbox, daytona, all

# Or with Ansible tags
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com -e email=admin@example.com \
  --tags gitlab
```

### 8.2 Connection Timeouts

```bash
# Increase Ansible timeout
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com -e email=admin@example.com \
  --timeout=300
```

### 8.3 Pod Issues

```bash
# Find unhealthy pods
kubectl get pods -A | grep -vE 'Running|Completed'

# Describe a failing pod
kubectl describe pod <pod-name> -n <namespace>

# View pod logs
kubectl logs <pod-name> -n <namespace> --tail=100

# Restart a deployment
kubectl rollout restart deployment/<name> -n <namespace>
```

### 8.4 Helm Release Issues

```bash
# List all Helm releases
helm list -A

# Check release status
helm status gitlab -n gitlab
helm status argocd -n argocd

# Rollback a release
helm rollback gitlab 1 -n gitlab

# Re-install a failed release
# The platform auto-detects failed releases and removes them before re-install
./platform.sh deploy gitlab
```

### 8.5 GitLab Not Ready After Deployment

GitLab takes 10–20 minutes to become fully ready after Helm install (the playbook
does NOT wait for GitLab readiness).

```bash
# Check GitLab pod status
kubectl get pods -n gitlab -w

# Wait for all pods to be Running
while kubectl get pods -n gitlab | grep -vE 'Running|Completed'; do sleep 10; done

# Check GitLab application status
helm status gitlab -n gitlab
```

### 8.6 TLS Certificate Issues

```bash
# Check cert-manager certificates
kubectl get certificates -A
kubectl get certificaterequests -A
kubectl get orders -A
kubectl get challenges -A

# Check certificate events
kubectl describe certificate <name> -n <namespace>

# Force certificate renewal
kubectl annotate certificate <name> -n <namespace> \
  cert-manager.io/webhook-last-prepare-time="" --overwrite
```

### 8.7 Vault Sealed

```bash
# Check vault status
kubectl exec -n vault vault-0 -- vault status

# If sealed, use the unseal keys from generate-secrets output
kubectl exec -n vault vault-0 -- vault operator unseal <key1>
kubectl exec -n vault vault-1 -- vault operator unseal <key1>
# Repeat for threshold number of nodes
```

### 8.8 ArgoCD Application Out of Sync

```bash
# Check sync status
kubectl get application <name> -n argocd -o jsonpath='{.status.sync.status}'
echo

# Manual sync
kubectl patch application <name> -n argocd \
  --type merge -p '{"spec": {"syncPolicy": {"automated": {"selfHeal": true, "prune": true}}}}'

# Or via ArgoCD CLI
argocd app sync <name>

# Check ArgoCD logs
kubectl logs -n argocd -l app.kubernetes.io/name=argocd-server --tail=50
```

### 8.9 DNS Not Resolving

```bash
# Verify Hetzner DNS records
hcloud server ip k8s-bastion
hcloud load-balancer describe k8s-lb -o json | jq '.public_net.ipv4.ip'

# Manually create DNS records pointing subdomain to LB IP
# Required records:
#   * A  LB_IP    (catch-all)
#   @ A  LB_IP    (apex)
#   vpn A  <bastion_IP>
#   gitlab A  LB_IP
#   argocd A  LB_IP
#   grafana A  LB_IP
#   vault A  LB_IP
#   s3 A  LB_IP
#   object-storage A  LB_IP
#   temporal A  LB_IP
#   kibana A  LB_IP
#   mail A  LB_IP
#   registry A  LB_IP
#   kas A  LB_IP
#   app A  LB_IP
#   api A  LB_IP
```

### 8.10 Auto-Heal

```bash
# Run the platform auto-heal command
./platform-orchestrator/platform.sh heal
```

This deletes pods that are not in Running or Completed state, forcing Kubernetes
to recreate them.

---

## 9. Rollback

### 9.1 Rollback a Specific Service

```bash
# Helm rollback
helm rollback <release> <revision> -n <namespace>

# Examples:
helm rollback gitlab 1 -n gitlab
helm rollback argocd 1 -n argocd
helm rollback seaweedfs 1 -n storage

# Kubernetes rollout undo
kubectl rollout undo deployment/<name> -n <namespace>
```

### 9.2 Rollback Entire Platform (Re-deploy from Scratch)

```bash
# 1. Tear down existing infrastructure
./platform-orchestrator/platform.sh destroy
# Confirm by typing: DESTROY

# OR via Ansible:
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com -e email=admin@example.com \
  -e state=absent

# 2. Back up secrets before destroying
cp playbooks/.platform-secrets.yml /secure/backups/

# 3. Re-run deployment
./platform.sh deploy all
# OR:
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com -e email=admin@example.com
```

### 9.3 Preserve Data During Rollback

Persistent volumes (PostgreSQL, Vault, SeaweedFS, GitLab Gitaly) survive
node and Helm rollbacks. Only the `destroy` / `state=absent` operation removes
storage volumes.

---

## 10. Teardown

### 10.1 Full Platform Destruction

```bash
# Warning: this removes ALL Hetzner resources and persistent volumes
cd platform-orchestrator
./platform.sh destroy

# Confirm by typing: DESTROY
```

Or via Ansible:

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -i inventory.yml \
  -e domain=example.com -e email=admin@example.com \
  -e state=absent
```

### 10.2 Manual Teardown

```bash
./teardown.sh k8s
```

This removes:
- Load balancer
- All servers (bastion, CP, workers)
- All volumes
- All firewalls
- SSH key
- Network and subnets

### 10.3 Post-Teardown

```bash
# Clean up local config
rm -f ~/.kube/config

# Remove secrets file if no longer needed
rm -f playbooks/.platform-secrets.yml

# DNS records remain — remove manually from your DNS provider
```

---

## 11. Appendix — Tier Comparison

| Resource | minimal | small | medium | production |
|----------|---------|-------|--------|------------|
| **Nodes** | 2 | 3 | 5 | 6+ |
| **Control planes** | 1 (schedulable) | 1 (schedulable) | 3 (HA) | 3 (HA) |
| **Workers** | 1 | 2 | 2 | 3 |
| **HA** | No | No | Yes (CP) | Yes (CP + workloads) |
| **Bastion type** | cx23 | cx23 | cx23 | cx23 |
| **CP/Worker type** | cx23 | cx23 | cx43 (16Gi) | cx43 (16Gi) |
| **LB** | Optional | lb11 | lb11 | lb11 |
| **Vault replicas** | 1 | 1 | 3 | 3 |
| **PostgreSQL replicas** | 1 | 1 | 2 | 3 |
| **Object storage** | 4×20Gi | 4×40Gi | 4×100Gi | 4×150Gi |
| **PostgreSQL storage** | 20Gi | 30Gi | 50Gi | 100Gi |
| **Metrics retention** | 7d | 14d | 30d | 30d |
| **Metrics storage** | 20Gi | 40Gi | 100Gi | 150Gi |
| **Log retention** | 3d | 7d | 14d | 14d |
| **Log stack** | Loki | Loki | ELK | ELK |
| **ArgoCD HA** | No | No | Yes | Yes |
| **Tempo tracing** | No | No | Yes | Yes |
| **Est. server cost/mo** | ~€16 | ~€22 | ~€85 | ~€101 |
| **Best for** | Dev/test | Demo | Small teams | Production |

---

## Quick Reference Card

```bash
# ═══ Essential Commands ═══

# Deploy
cd platform-orchestrator && ./platform.sh deploy all

# Status
./platform.sh status

# Credentials
./platform.sh credentials

# Health check
./platform.sh health

# Auto-heal
./platform.sh heal

# Re-deploy a component
./platform.sh deploy gitlab

# Destroy everything
./platform.sh destroy

# ═══ Kubernetes ═══
kubectl get nodes
kubectl get pods -A
kubectl get pods -A | grep -vE 'Running|Completed'

# ═══ Hetzner ═══
hcloud server list
hcloud load-balancer list
hcloud network list

# ═══ Secrets ═══
cat playbooks/.platform-secrets.yml

# ═══ Logs ═══
kubectl logs -n production -l app=nest-react-boilerplate --tail=100
```
