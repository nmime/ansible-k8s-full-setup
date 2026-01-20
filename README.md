# Ansible K8s Full-Stack Platform Setup

Complete Ansible workflow for deploying a full-stack Kubernetes platform with all services enabled by default.

## Overview

This Ansible playbook automates the deployment of a comprehensive Kubernetes platform including:

- **Infrastructure**: Hetzner Cloud servers, networks, load balancers
- **Kubernetes Cluster**: Kubespray-based cluster with Cilium, Gateway API, cert-manager
- **Security**: Headscale VPN, TLS certificates, firewall rules
- **Secrets**: HashiCorp Vault + External Secrets Operator
- **Storage**: MinIO S3-compatible storage
- **Databases**: PostgreSQL and MongoDB via Percona Operators
- **GitLab**: Self-hosted GitLab with runners and registry
- **GitOps**: ArgoCD for continuous delivery
- **Observability**: VictoriaMetrics, Loki, Grafana
- **Autoscaling**: KEDA for event-driven scaling

## Prerequisites

### Required Credentials (Configured via splox secrets)
- `HETZNER_API_TOKEN`: Hetzner Cloud API access
- `SSH_PRIVATE_KEY` / `SSH_PUBLIC_KEY`: SSH key pairs
- `POSTGRES_PASSWORD`: PostgreSQL admin password
- `MONGODB_ROOT_PASSWORD`: MongoDB admin password
- `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`: MinIO credentials
- `GITLAB_ROOT_PASSWORD`: GitLab root password
- `GRAFANA_ADMIN_PASSWORD`: Grafana admin password
- `VAULT_ROOT_TOKEN`: HashiCorp Vault token
- `ARGOCD_ADMIN_PASSWORD`: ArgoCD admin password
- `LETSENCRYPT_EMAIL`: TLS certificate notifications
- `GITHUB_TOKEN`: GitHub authentication

### System Requirements
- Ansible 2.15+ / 2.16+
- Python 3.9+
- Access to Hetzner Cloud API
- SSH access to target servers

## Quick Start

```bash
# Clone the repository
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup

# Create inventory file
cp inventory.example inventory

# Edit inventory with your configuration
vim inventory

# Run the playbook
ansible-playbook playbooks/deploy_platform.yml -i inventory
```

## Architecture

### Network Architecture
```
INTERNET
    │
    ├─ PUBLIC (via LB) ───▶ app, api, s3, registry
    │
    └─ ADMIN (via VPN) ──▶ gitlab, argocd, grafana, vault, k8s
                │
                └──▶ Bastion + Headscale
```

### Deployment Order
1. Infrastructure (Hetzner servers, networks, LB)
2. DNS Configuration
3. Kubernetes Cluster (Kubespray)
4. TLS Certificates (cert-manager)
5. Storage (MinIO)
6. Secrets (Vault + ESO)
7. Databases (PostgreSQL, MongoDB)
8. GitLab
9. GitOps (ArgoCD)
10. Observability
11. Autoscaling

## Services

### Public Services (via Load Balancer)
- `app.example.com` - Frontend application
- `api.example.com` - Backend API
- `s3.example.com` - MinIO S3 API
- `registry.example.com` - Container Registry

### Private Services (via VPN)
- `gitlab.example.com` - GitLab CE
- `argocd.example.com` - ArgoCD
- `grafana.example.com` - Grafana monitoring
- `minio.example.com` - MinIO Console
- `vault.example.com` - HashiCorp Vault
- `k8s.example.com` - Kubernetes API

## Configuration

Edit `inventory` file to customize:

### Global Settings
```ini
[global]
domain = example.com
email = admin@example.com
environment = production
```

### Infrastructure
```ini
[hetzner]
control_plane_count = 3
worker_count = 3
bastion_server_type = cx22
worker_server_type = cpx41
```

### Kubernetes
```ini
[kubernetes]
version = v1.34.3
cni = cilium
enable_gateway_api = true
```

## Deployment Tiers

### Minimal (~€12/mo)
- 2 nodes (1 master, 1 worker)
- No HA
- Light mode services

### Small (~€21/mo)
- 3 nodes (1 master, 2 workers)
- No HA
- Standard services

### Medium (~€34/mo)
- 5 nodes (3 masters HA, 2 workers)
- Full HA for control plane
- All services enabled

### Production (~€48/mo)
- 6 nodes (3 masters HA, 3 workers)
- Full HA everywhere
- Maximum redundancy

## Verification

After deployment, verify services:

```bash
# Check nodes
kubectl get nodes

# Check pods (all namespaces)
kubectl get pods -A

# Verify services are running
kubectl get svc -A

# Check GitLab
curl -k https://gitlab.example.com

# Check ArgoCD
curl -k https://argocd.example.com

# Check Grafana
curl -k https://grafana.example.com
```

## Troubleshooting

See `docs/troubleshooting.md` for common issues and solutions.

## Architecture Details

- **Control Plane**: 3-node HA (etcd + K8s API + Controllers)
- **Worker Nodes**: Scalable pods with HPA/KEDA
- **Storage**: 4x MinIO replicas (distributed mode)
- **Databases**: PostgreSQL HA cluster + MongoDB ReplicaSet
- **Monitoring**: VictoriaMetrics (30d retention) + Loki (14d) + Grafana

## License

MIT

## Support

For issues and questions, please open an issue on GitHub.
