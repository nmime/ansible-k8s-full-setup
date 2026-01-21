# Ansible Kubernetes Full-Stack Setup

A comprehensive Ansible workflow for deploying a complete Kubernetes platform stack including:

- Hetzner Cloud Infrastructure
- Kubernetes Cluster (via Kubespray)
- Network Security (Headscale VPN + Firewalls)
- Secrets Management (Vault + ESO)
- Databases (PostgreSQL + MongoDB)
- Storage (MinIO S3)
- GitLab CE (Self-hosted)
- ArgoCD (GitOps)
- Observability (VictoriaMetrics + Loki + Grafana)
- Event-Driven Autoscaling (KEDA)

## Quick Start

### 1. Clone the Repository
```bash
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup
```

### 2. Install Ansible and Requirements
```bash
pip install ansible-core==2.16.0
ansible-galaxy collection install -r requirements.yml
```

### 3. Customize Inventory
```bash
cp inventory.example inventory
vim inventory
```

### 4. Deploy
```bash
ansible-playbook playbooks/deploy_platform.yml -i inventory
```

## Tiers

- **minimal**: 2 nodes (~€12/mo)
- **small**: 3 nodes (~€21/mo)
- **medium**: 5 nodes (~€34/mo, HA enabled)
- **production**: 6 nodes (~€48/mo, HA enabled)

## Services

| Service | Port | Description |
|---------|------|-------------|
| GitLab  | 443  | Self-hosted GitLab |
| ArgoCD  | 443  | GitOps Controller |
| Grafana | 3000 | Monitoring Dashboard |
| MinIO   | 9000 | S3 Object Storage |
| Vault   | 8200 | Secrets Manager |

## Documentation

- **DEPLOYMENT.md**: Detailed deployment and testing guide
- **docs/**: Additional documentation (when created)

## Requirements

- Hetzner API Token (`HETZNER_API_TOKEN`)
- SSH Keys (`SSH_PRIVATE_KEY`, `SSH_PUBLIC_KEY`)
- DNS Domain configured

## Support

For issues and questions, please open an issue on GitHub.

## License

MIT License
