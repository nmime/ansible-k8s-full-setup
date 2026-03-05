# Roles Directory

This directory contains 11 Ansible roles for each platform component:

| Role | Description | Key Technologies | Version |
|------|-------------|-----------------|---------|
| `hetzner-infra/` | Cloud infrastructure provisioning | hcloud CLI, Ubuntu 24.04 | - |
| `network-security/` | VPN and bastion hardening | Headscale, Tailscale, UFW, fail2ban | v0.28.0 |
| `k8s-cluster-management/` | Kubernetes cluster installation | Kubespray, Cilium, Gateway API, cert-manager, MetalLB | K8s v1.34.3, Cilium v1.18.6 |
| `k8s-secrets/` | Secrets management | Vault (HA Raft), External Secrets Operator | Vault v1.21.2, ESO v0.15.0 |
| `minio-storage/` | S3-compatible object storage | Bitnami Helm, Chainguard image | Chart 17.0.21 |
| `k8s-databases/` | Database deployments | Percona PG Operator, Percona MongoDB Operator | PG v2.8.2, PSMDB v1.21.2 |
| `gitlab-selfhosted/` | GitLab CE with CI/CD | Helm, GitLab Runner | CE v18.7.1, Chart 9.7.1 |
| `k8s-gitops/` | GitOps continuous delivery | ArgoCD, ApplicationSets | v3.2.5, Chart 7.8.5 |
| `k8s-observability/` | Monitoring, logging, dashboards | VictoriaMetrics, Loki, Grafana | VM v1.133.0, Loki v3.6.3, Grafana v12.3.1 |
| `k8s-autoscaling/` | Event-driven autoscaling | KEDA (70+ scalers) | v2.18.2 |
| `brocoders-boilerplate-setup/` | Full-stack application setup | NestJS, React, ArgoCD | - |

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
