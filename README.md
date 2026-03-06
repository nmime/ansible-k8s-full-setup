# Ansible Kubernetes Full-Stack Setup

A comprehensive Ansible workflow for deploying a complete Kubernetes platform stack on Hetzner Cloud — from bare infrastructure to running applications with GitOps, monitoring, and automated backups.

## Architecture

```
                    ┌─────────────────────────────────────────────────────────┐
                    │                    Internet                              │
                    └──────────┬──────────────────┬───────────────────────────┘
                               │                  │
                    ┌──────────▼──────┐  ┌────────▼─────────┐
                    │  Hetzner LB     │  │  Bastion (VPN)   │
                    │  :80 → :30080   │  │  Headscale :443  │
                    │  :443 → :30443  │  │  SSH :22         │
                    └──────────┬──────┘  └────────┬─────────┘
                               │                  │
                    ┌──────────▼──────────────────▼───────────────────────────┐
                    │              Private Network (10.0.0.0/16)               │
                    │                                                          │
                    │  ┌─────────────────┐  ┌─────────────────┐               │
                    │  │  Control Plane   │  │  Worker Nodes    │              │
                    │  │  10.0.1.0/24     │  │  10.0.2.0/24     │             │
                    │  │  (no public IP)  │  │  (no public IP)  │             │
                    │  └─────────────────┘  └─────────────────┘               │
                    │                                                          │
                    │  Public Gateway (main-gateway)                           │
                    │    → app.domain.com     (Frontend)                       │
                    │    → api.domain.com     (Backend API)                    │
                    │    → s3.domain.com      (MinIO S3 API)                   │
                    │    → registry.domain.com (Container Registry)            │
                    │                                                          │
                    │  Admin Gateway (admin-gateway, VPN-only)                 │
                    │    → gitlab.domain.com   (GitLab CI/CD)                  │
                    │    → argocd.domain.com   (GitOps)                        │
                    │    → grafana.domain.com  (Monitoring)                    │
                    │    → vault.domain.com    (Secrets)                       │
                    │    → minio.domain.com    (Storage Console)               │
                    │    → pmm.domain.com      (DB Monitoring)                │
                    └──────────────────────────────────────────────────────────┘
```

### Network Security Model

- **All Kubernetes nodes** are private-only (no public IPs)
- **Bastion host** is the only public entry point (SSH + VPN)
- **Outbound NAT** is handled by the bastion for private nodes
- **Public services** (app, API, S3, registry) are exposed via Hetzner Load Balancer → main-gateway
- **Admin services** (GitLab, ArgoCD, Grafana, Vault) are VPN-only via admin-gateway with CiliumNetworkPolicy restricting to `10.0.0.0/16` + `100.64.0.0/10` (Headscale VPN range)
- **Databases** (PostgreSQL, MongoDB) are cluster-internal only

## Quick Start

### Prerequisites

| Tool | Version | Install |
|------|---------|---------|
| Ansible | >= 2.16.0 | `pip install ansible-core` |
| hcloud CLI | latest | [github.com/hetznercloud/cli](https://github.com/hetznercloud/cli/releases) |
| kubectl | >= 1.28 | [kubernetes.io/docs/tasks/tools](https://kubernetes.io/docs/tasks/tools/) |
| helm | >= 3.14 | [helm.sh/docs/intro/install](https://helm.sh/docs/intro/install/) |
| yq | >= 4.0 | `snap install yq` or [github.com/mikefarah/yq](https://github.com/mikefarah/yq/releases) |
| SSH key | Ed25519 | `ssh-keygen -t ed25519` |

### Environment Variables

```bash
# Required (used for servers, networks, DNS, and all Hetzner services)
export HCLOUD_TOKEN="your-hetzner-cloud-api-token"
# Get from: https://console.hetzner.cloud → Security → API Tokens

# Optional (auto-generated if not set)
export MINIO_ROOT_USER="minioadmin"         # MinIO admin username
export MINIO_ROOT_PASSWORD="your-password"  # MinIO admin password
```

### Option A: Platform Orchestrator (Recommended)

```bash
cd platform-orchestrator
./platform.sh init                # Creates platform.yaml from small profile
vim platform.yaml                 # Set your domain, project name, tier
./platform.sh deploy all          # Full deployment
```

### Option B: Ansible Directly

```bash
pip install ansible-core>=2.16.0
ansible-galaxy collection install -r requirements.yml

cp inventory.example inventory.yml
vim inventory.yml                 # Set domain, project, tier

export HCLOUD_TOKEN="your-token"
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml
```

### Option C: Component-by-Component

```bash
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags infra
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags network
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags cluster
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags secrets
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags storage
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags databases
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags gitlab
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags gitops
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags observability
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags autoscaling
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags boilerplate
```

## Platform Orchestrator Commands

```bash
./platform.sh init                    # Initialize platform.yaml from profile
./platform.sh deploy all              # Deploy everything
./platform.sh deploy <component>      # Deploy specific component
./platform.sh status                  # Show cluster and service status
./platform.sh health                  # Health check all components
./platform.sh credentials             # Display all service credentials
./platform.sh heal                    # Attempt to fix unhealthy components
./platform.sh destroy                 # Tear down the entire platform
```

## Deployment Tiers

| Tier | Nodes | HA | Cost | Best For |
|------|-------|----|------|----------|
| minimal | 2 (1 master+worker, 1 worker) | No | ~€18-20/mo | Development, learning |
| small | 3 (1 master, 2 workers) | No | ~€28-35/mo | Startups, staging |
| medium | 5 (3 masters, 2 workers) | Yes | ~€48-55/mo | Small-medium traffic |
| production | 6+ (3 masters, 3+ workers) | Full | ~€74-100/mo | Production workloads |

## Services

| Service | Access | URL | Description |
|---------|--------|-----|-------------|
| App Frontend | Public | `app.example.com` | React SPA |
| App Backend | Public | `api.example.com` | NestJS API |
| MinIO S3 API | Public | `s3.example.com` | S3-compatible storage |
| Container Registry | Public | `registry.example.com` | Docker image registry |
| GitLab | VPN-only | `gitlab.example.com` | Self-hosted CI/CD |
| ArgoCD | VPN-only | `argocd.example.com` | GitOps controller |
| Grafana | VPN-only | `grafana.example.com` | Monitoring dashboards |
| Vault | VPN-only | `vault.example.com` | Secrets management |
| MinIO Console | VPN-only | `minio.example.com` | S3 admin console |
| PMM | VPN-only | `pmm.example.com` | Percona DB monitoring |
| Headscale VPN | Public | `vpn.example.com` | VPN entry point |

## Secrets Management

All credentials are **auto-generated once** by the `generate-secrets` role and persisted to `.platform-secrets.yml` (gitignored). This ensures:

- Credentials are never hardcoded in playbooks
- Re-runs use the same passwords (idempotent)
- All roles share consistent credentials

Generated secrets include: MinIO root password, Grafana admin password, app database password, JWT secrets, ArgoCD admin password, and Headscale API key.

**Retrieve credentials post-deployment:**
```bash
./platform.sh credentials            # Via orchestrator
# Or manually:
cat .platform-secrets.yml            # All auto-generated secrets
kubectl get secret -n gitlab gitlab-gitlab-initial-root-password -o jsonpath='{.data.password}' | base64 -d
kubectl get secret -n argocd argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d
```

## GitLab EE License

GitLab is deployed as **Enterprise Edition (Ultimate)** with an auto-generated license:

- The `gitlab-selfhosted` role uses the `gitlab-license` Ruby gem to generate a 2048-bit RSA key pair and Ultimate license
- The generated `public.key` replaces GitLab's `.license_encryption_key.pub` via volume mount
- The license is applied via the GitLab Rails console on the toolbox pod
- License files are stored locally in `.gitlab-license/` (gitignored)
- On re-runs, existing license files are reused (idempotent)

**Prerequisites on the Ansible controller:**
```bash
# Ruby and gem must be installed
apt install ruby-full    # Debian/Ubuntu
gem install gitlab-license
```

## DNS Configuration

After deployment, configure DNS records pointing to the Hetzner Load Balancer IP:

```
*.example.com    →  <load-balancer-ip>  (A record)
example.com      →  <load-balancer-ip>  (A record)
vpn.example.com  →  <bastion-ip>        (A record)
```

The load balancer IP is shown in the deployment output, or via:
```bash
hcloud load-balancer list
```

TLS certificates are automatically issued by cert-manager using DNS01 challenges via the Hetzner DNS API.

## Deployment Flow

```
1. generate-secrets    → Generate & persist all credentials
2. hetzner-infra       → VPC, subnets, firewalls, servers, load balancer
3. network-security    → Bastion hardening, NAT gateway, Headscale VPN
4. k8s-cluster-management → Kubespray, Cilium CNI, Gateway API, cert-manager
5. k8s-secrets         → Vault HA, External Secrets Operator
6. minio-storage       → MinIO S3 (standalone or distributed by tier)
7. k8s-databases       → PostgreSQL + MongoDB via Percona Operators
8. gitlab-selfhosted   → GitLab EE (Ultimate) + Runner + Registry + License
9. k8s-gitops          → ArgoCD + ApplicationSets
10. k8s-observability  → VictoriaMetrics + Loki + Grafana + PMM Server
11. k8s-autoscaling    → KEDA event-driven autoscaling
12. brocoders-boilerplate → NestJS backend + React frontend
```

## Project Structure

```
├── .gitignore                         # Excludes secrets files
├── ansible.cfg                        # Ansible configuration
├── requirements.yml                   # Ansible collections
├── inventory.example                  # Inventory template
├── defaults/main.yml                  # Global defaults (loaded via vars_files)
├── playbooks/
│   └── deploy_platform.yml            # Main orchestration playbook
├── roles/
│   ├── generate-secrets/              # Centralized credential generation
│   ├── hetzner-infra/                 # Cloud infrastructure (VPC, servers, LB)
│   ├── network-security/              # Bastion, VPN, firewalls, NAT gateway
│   ├── k8s-cluster-management/        # Kubespray, Cilium, Gateway API, cert-manager
│   ├── k8s-secrets/                   # HashiCorp Vault + ESO
│   ├── minio-storage/                 # MinIO S3-compatible storage
│   ├── k8s-databases/                 # PostgreSQL + MongoDB (Percona Operators)
│   ├── gitlab-selfhosted/             # GitLab EE (Ultimate) + License + Runner + Registry
│   ├── k8s-gitops/                    # ArgoCD + ApplicationSets
│   ├── k8s-observability/             # VictoriaMetrics + Loki + Grafana
│   ├── k8s-autoscaling/               # KEDA event-driven autoscaler
│   └── brocoders-boilerplate-setup/   # NestJS + React application
└── platform-orchestrator/
    ├── platform.sh                    # CLI orchestrator
    ├── platform.example.yaml          # Configuration template
    └── profiles/                      # Tier configuration profiles
        ├── minimal.yaml
        ├── small.yaml
        ├── medium.yaml
        └── production.yaml
```

## Pre-flight Checks

The playbook automatically validates before deployment:

- **hcloud CLI** is installed and accessible
- **HCLOUD_TOKEN** environment variable is set
- **SSH key** exists at the configured path (`~/.ssh/id_ed25519` by default)
- **kubectl** is installed (warning only — not blocking)
- **Tier** is valid (minimal, small, medium, production)

## VPN Access (Admin Services)

After deployment, connect to the Headscale VPN to access admin services:

```bash
# Install Tailscale client
curl -fsSL https://tailscale.com/install.sh | sh

# Connect to your Headscale server
tailscale up --login-server https://vpn.example.com

# Admin services are now accessible:
# https://gitlab.example.com
# https://argocd.example.com
# https://grafana.example.com
# https://vault.example.com
# https://minio.example.com
```

## Backup & Restore

- **PostgreSQL**: Automated via pgbackrest (daily full + 6-hourly differential + weekly S3) to local volume + MinIO S3, retention: 7 full / 4 diff
- **MongoDB**: Automated via Percona Backup for MongoDB (daily logical + weekly physical + PITR/oplog) to MinIO S3
- **GitLab**: Built-in backup to `gitlab-backups` MinIO bucket
- **Vault**: Auto-unseal with stored init keys; data persisted on `hcloud-volumes`

## Database Monitoring (Percona PMM)

All databases are monitored via **Percona Monitoring and Management (PMM)**:

- **PMM Server**: Deployed in the monitoring namespace, accessible via VPN at `pmm.example.com`
- **PostgreSQL**: PMM client sidecar with `pg_stat_statements`, `pg_stat_monitor` extensions enabled
- **MongoDB**: PMM client sidecar with all collectors enabled
- **Metrics pipeline**: PMM metrics are also scraped by VictoriaMetrics via VMServiceScrape
- **Grafana dashboards**: PostgreSQL overview, MongoDB overview, replication monitoring, PMM datasource
- **Alerts**: Database-specific alerting rules for downtime, replication lag, connections, deadlocks, backup staleness

## Troubleshooting

```bash
# Check platform health
./platform.sh health

# Attempt auto-repair
./platform.sh heal

# Check specific component
kubectl get pods -n gitlab
kubectl get pods -n argocd
kubectl get pods -n monitoring
kubectl get pods -n vault
kubectl get pods -n storage
kubectl get pods -n databases

# Check Gateway routes
kubectl get httproutes -A
kubectl get gateways -n cilium-system

# Check certificates
kubectl get certificates -A
kubectl get certificaterequests -A
```

## Documentation

- **DEPLOYMENT.md**: Detailed deployment testing and verification guide
- **roles/README.md**: Role-level documentation with versions and technologies

## License

MIT License
