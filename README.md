# Ansible Kubernetes Full-Stack Setup

A comprehensive Ansible workflow for deploying a complete Kubernetes platform stack on Hetzner Cloud.

## Architecture

```
Platform Orchestrator
├── Infrastructure (Hetzner Cloud)
├── Network Security (Headscale VPN + Firewalls)
├── Kubernetes Cluster (Kubespray + Cilium + Gateway API)
├── Secrets Management (Vault + External Secrets Operator)
├── Storage (MinIO S3-compatible)
├── Databases (PostgreSQL HA via Percona Operator)
├── GitLab CE (Self-hosted CI/CD + Registry)
├── ArgoCD (GitOps Continuous Delivery)
├── Observability (VictoriaMetrics + Loki + Grafana)
├── Autoscaling (KEDA Event-Driven)
└── Application Boilerplate (NestJS + React)
```

## Quick Start

### 1. Install Requirements
```bash
pip install ansible-core>=2.16.0
ansible-galaxy collection install -r requirements.yml
```

### 2. Configure
```bash
# Option A: Use platform orchestrator
cd platform-orchestrator
./platform.sh init  # Creates platform.yaml from small profile
vim platform.yaml   # Set domain, project, tier
./platform.sh deploy all

# Option B: Use Ansible directly
cp inventory.example inventory.yml
vim inventory.yml   # Customize settings
export HCLOUD_TOKEN="your-token"
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml
```

### 3. Deploy individual components
```bash
# Deploy specific components using tags
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags gitlab
ansible-playbook playbooks/deploy_platform.yml -i inventory.yml --tags observability
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
| GitLab | VPN | `gitlab.example.com` | Self-hosted CI/CD |
| Registry | Public | `registry.example.com` | Container Registry |
| ArgoCD | VPN | `argocd.example.com` | GitOps Controller |
| Grafana | VPN | `grafana.example.com` | Monitoring Dashboards |
| MinIO Console | VPN | `minio.example.com` | S3 Storage Admin |
| MinIO API | Public | `s3.example.com` | S3 API |
| Vault | VPN | `vault.example.com` | Secrets Manager |
| VPN | Public | `vpn.example.com` | Headscale VPN |

## Project Structure

```
├── ansible.cfg                    # Ansible configuration
├── requirements.yml               # Ansible collections
├── inventory.example              # Inventory template
├── defaults/main.yml              # Global defaults
├── playbooks/
│   └── deploy_platform.yml        # Main orchestration playbook
├── roles/
│   ├── hetzner-infra/             # Cloud infrastructure
│   ├── network-security/          # VPN + firewalls
│   ├── k8s-cluster-management/    # Kubernetes cluster
│   ├── k8s-secrets/               # Vault + ESO
│   ├── minio-storage/             # S3 storage
│   ├── k8s-databases/             # PostgreSQL + MongoDB
│   ├── gitlab-selfhosted/         # GitLab CE
│   ├── k8s-gitops/                # ArgoCD
│   ├── k8s-observability/         # Metrics + Logs + Dashboards
│   ├── k8s-autoscaling/           # KEDA
│   └── brocoders-boilerplate-setup/ # Application boilerplate
└── platform-orchestrator/
    ├── platform.sh                # CLI orchestrator
    ├── platform.example.yaml      # Configuration template
    └── profiles/                  # Tier profiles
        ├── minimal.yaml
        ├── small.yaml
        ├── medium.yaml
        └── production.yaml
```

## Requirements

- Hetzner Cloud API Token (`HCLOUD_TOKEN`)
- SSH key pair (Ed25519 recommended)
- DNS domain configured
- Ansible >= 2.16.0
- kubectl, helm, yq (for platform orchestrator)

## Documentation

- **DEPLOYMENT.md**: Detailed deployment and testing guide
- **roles/README.md**: Role structure documentation

## License

MIT License
