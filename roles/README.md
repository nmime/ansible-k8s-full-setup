# Roles Directory

This directory contains Ansible roles for each platform component.

| Role | Description | Key Technologies | Version |
|------|-------------|------------------|---------|
| `generate-secrets/` | Centralized credential generation and persistence | Ansible lookups | - |
| `hetzner-infra/` | Cloud infrastructure provisioning | hcloud CLI, Ubuntu 24.04 | - |
| `network-security/` | VPN and bastion hardening | Headscale, Tailscale, UFW, fail2ban | Headscale v0.28.0 |
| `k8s-cluster-management/` | Kubernetes cluster installation | Kubespray, Cilium, Gateway API, cert-manager, MetalLB, Hetzner CCM/CSI | K8s v1.34.3, Cilium v1.19.2 |
| `k8s-secrets/` | Secrets management | Vault (HA Raft), External Secrets Operator | Vault Chart 0.32.0, ESO Chart 2.2.0 |
| `minio-storage/` | S3-compatible object storage | MinIO official Helm chart | Chart 5.4.0 |
| `k8s-databases/` | Database deployments | Percona PG Operator, Percona MongoDB Operator | PostgreSQL 18, MongoDB 8.0 |
| `gitlab-selfhosted/` | GitLab CE | Helm, GitLab Runner | CE v18.10.0, Chart 9.10.0 |
| `k8s-gitops/` | GitOps continuous delivery | ArgoCD, ApplicationSets | Chart 9.4.17 |
| `k8s-observability/` | Monitoring, logging, dashboards, alerting | VictoriaMetrics, Loki, Grafana, PMM | - |
| `k8s-autoscaling/` | Event-driven autoscaling | KEDA | Chart 2.19.0 |
| `temporal/` | Workflow orchestration engine | Temporal Server, Web UI | Chart 0.73.2 |
| `dragonfly/` | Redis-compatible in-memory store | Dragonfly Operator, Dragonfly CRD | Operator v1.5.0, Dragonfly v1.37.2 |
| `postal/` | Mail server | Postal, MariaDB, Dragonfly | v3.3.5 |
| `blackbox-exporter/` | Uptime and synthetic probes | prometheus-blackbox-exporter, VMProbe | Chart 9.0.1 |
| `daytona-deployment/` | Optional workspace platform | Daytona Helm chart | Chart 0.0.23 |

## Deployment Order

Roles are deployed sequentially to respect dependencies:

```
 1. generate-secrets        Generate all platform credentials (idempotent)
 2. hetzner-infra           Provision VPC, subnets, firewalls, bastion, nodes, LB, DNS
 3. network-security        Bastion hardening, NAT gateway, Headscale VPN
 4. k8s-cluster-management  Kubespray K8s install, Cilium, Gateway API, cert-manager, CCM/CSI
 5. k8s-secrets             Vault, auto-init/unseal, ESO + ClusterSecretStore
 6. minio-storage           MinIO S3, pre-created buckets
 7. k8s-observability       VictoriaMetrics, Loki, Grafana, PMM, alerting
 8. k8s-databases           PostgreSQL 18 HA + MongoDB 8.0
 9. gitlab-selfhosted       GitLab CE + Runner + Registry + KAS
10. k8s-gitops              ArgoCD and ApplicationSets
11. k8s-autoscaling         KEDA event-driven autoscaler
12. dragonfly               Dragonfly in-memory store
13. temporal                Temporal workflow engine + Web UI
14. postal                  Postal mail server
15. blackbox-exporter       Synthetic probes
16. daytona-deployment      Daytona workspace platform (optional, deploy_daytona=true)
```

## Tier Support

All core roles support four deployment tiers: minimal, small, medium, and production.

## Usage

```yaml
# Deploy full platform
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=small -e domain=example.com -e email=admin@example.com

# Deploy Daytona explicitly
ansible-playbook playbooks/deploy_platform.yml \
  --tags daytona -e deploy_daytona=true -e domain=example.com -e email=admin@example.com
```

## Legacy role directories pending deletion

The former workspace/sandbox role directories are intentionally left in this branch for a follow-up deletion-only action:

- `roles/opwerf-deployment/`
- `roles/e2b-deployment/`
