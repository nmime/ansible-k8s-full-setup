# Roles Directory

This directory contains 11 Ansible roles for each platform component:

| Role | Description | Key Technologies |
|------|-------------|-----------------|
| `hetzner-infra/` | Cloud infrastructure provisioning | hcloud CLI |
| `network-security/` | VPN and firewall configuration | Headscale, iptables |
| `k8s-cluster-management/` | Kubernetes cluster installation | Kubespray, Cilium, Gateway API |
| `k8s-secrets/` | Secrets management | Vault, External Secrets Operator |
| `minio-storage/` | S3-compatible object storage | MinIO |
| `k8s-databases/` | Database deployments | PostgreSQL (Percona), MongoDB |
| `gitlab-selfhosted/` | GitLab CE with CI/CD | Helm, GitLab Runner |
| `k8s-gitops/` | GitOps continuous delivery | ArgoCD, ApplicationSets |
| `k8s-observability/` | Monitoring, logging, dashboards | VictoriaMetrics, Loki, Grafana |
| `k8s-autoscaling/` | Event-driven autoscaling | KEDA (70+ scalers) |
| `brocoders-boilerplate-setup/` | Full-stack application setup | NestJS, React, Helm |

## Deployment Order

Roles are deployed sequentially to respect dependencies:

1. Infrastructure (Hetzner servers, networks, firewalls)
2. Network Security (VPN, firewall rules)
3. Kubernetes Cluster (Kubespray, Cilium CNI, cert-manager)
4. Secrets Management (Vault HA, ESO)
5. Storage (MinIO S3)
6. Databases (PostgreSQL HA, MongoDB)
7. GitLab (CE + Runner + Registry)
8. GitOps (ArgoCD + ApplicationSets)
9. Observability (VictoriaMetrics + Loki + Grafana)
10. Autoscaling (KEDA)
11. Application Boilerplate (optional)

## Tier Support

All roles support four deployment tiers:
- **minimal**: Single replicas, minimal resources
- **small**: Single replicas, standard resources
- **medium**: HA control plane, distributed storage
- **production**: Full HA, multiple replicas, production resources

## Usage

```yaml
# Include specific role
- ansible.builtin.include_role:
    name: gitlab-selfhosted
  vars:
    tier: medium
    domain: example.com

# Deploy with tags
ansible-playbook playbooks/deploy_platform.yml --tags gitlab
```
