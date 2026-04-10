# Roles Directory

This directory contains 15 Ansible roles for each platform component:

| Role | Description | Key Technologies | Version |
|------|-------------|-----------------|---------|
| `generate-secrets/` | Centralized credential generation and persistence | Ansible lookups | - |
| `hetzner-infra/` | Cloud infrastructure provisioning | hcloud CLI, Ubuntu 24.04 | - |
| `network-security/` | VPN and bastion hardening | Headscale, Tailscale, UFW, fail2ban | Headscale v0.28.0 |
| `k8s-cluster-management/` | Kubernetes cluster installation | Kubespray, Cilium, Gateway API, cert-manager, MetalLB, Hetzner CCM/CSI | K8s v1.34.3, Cilium v1.19.2, cert-manager v1.20.1, MetalLB v0.15.3, CCM v1.30.1, CSI v2.20.0 |
| `k8s-secrets/` | Secrets management | Vault (HA Raft), External Secrets Operator | Vault Chart 0.32.0, ESO Chart 2.2.0 |
| `minio-storage/` | S3-compatible object storage | MinIO official Helm chart | Chart 5.4.0 |
| `k8s-databases/` | Database deployments | Percona PG Operator, Percona MongoDB Operator | PG Op v2.8.2, PSMDB Op v1.22.0, PostgreSQL 18, MongoDB 8.0 |
| `gitlab-selfhosted/` | GitLab CE | Helm, GitLab Runner | CE v18.10.0, Chart 9.10.0, Runner 0.87.0 |
| `k8s-gitops/` | GitOps continuous delivery | ArgoCD, ApplicationSets | ArgoCD v3.3.6, Chart 9.4.17 |
| `k8s-observability/` | Monitoring, logging, dashboards, alerting | VictoriaMetrics, Loki, Promtail, Grafana, PMM | VM Op 0.59.3, Loki 6.55.0, Grafana 10.5.15 |
| `k8s-autoscaling/` | Event-driven autoscaling | KEDA (70+ scalers) | Chart 2.19.0 |
| `temporal/` | Workflow orchestration engine | Temporal Server, Web UI, Admin Tools | v1.29.1, Chart 0.73.2 |
| `brocoders-boilerplate-setup/` | Full-stack application setup (optional) | NestJS, React, ArgoCD | - |
| `dragonfly/` | Redis-compatible in-memory store | Dragonfly Operator, Dragonfly CRD | Operator v1.5.0, Dragonfly v1.37.2 |
| `opwerf-deployment/` | AI-powered workflow orchestration (optional) | OpenWerf Dashboard, API, Worker, Elasticsearch | - |
| `e2b-deployment/` | Self-hosted code execution sandboxes (optional) | E2B, Firecracker, KVM, ClickHouse | E2B v0.1.4 |

## Deployment Order

Roles are deployed sequentially to respect dependencies:

```
 1. generate-secrets        Generate all platform credentials (idempotent)
 2. hetzner-infra           Provision VPC, subnets, firewalls, bastion, nodes, LB, DNS
 3. network-security        Bastion hardening, NAT gateway, Headscale VPN
 4. k8s-cluster-management  Kubespray K8s install, Cilium, Gateway API, cert-manager, CCM/CSI
 5. k8s-secrets             Vault (standalone/HA), auto-init/unseal, ESO + ClusterSecretStore
 6. minio-storage           MinIO S3 (standalone/distributed), pre-created buckets
 7. k8s-observability       VictoriaMetrics, Loki, Promtail, Grafana (12 dashboards), PMM, alerting
 8. k8s-databases           PostgreSQL 18 HA + PgBouncer + pgBackRest, MongoDB 8.0 + PBM
 9. gitlab-selfhosted       GitLab CE + Runner + Registry + KAS
10. k8s-gitops              ArgoCD (standalone/HA), multi-env ApplicationSet
11. k8s-autoscaling         KEDA event-driven autoscaler
12. temporal                Temporal workflow engine + Web UI
13. dragonfly              Dragonfly in-memory store (Redis-compatible, operator + instance)
14. brocoders-boilerplate   Sample NestJS+React app (optional, deploy_boilerplate=true)
15. opwerf-deployment       OpenWerf AI workflow platform (optional, deploy_opwerf=true)
16. e2b-deployment          E2B sandbox infrastructure on bare metal (optional, deploy_e2b=true)
```

## Tier Support

All roles support four deployment tiers:

| Tier | Nodes | HA | Cost | Use case |
|------|-------|----|------|----------|
| **minimal** | 2 | No | ~€18–20/mo | Dev, learning, testing |
| **small** | 3 | No | ~€28–35/mo | Startups, staging |
| **medium** | 5 | Yes | ~€48–55/mo | Small-medium teams |
| **production** | 6+ | Yes | ~€74–100/mo | Production workloads |

## Usage

```yaml
# Deploy full platform
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=small -e domain=example.com -e email=admin@example.com

# Deploy specific components via tags
ansible-playbook playbooks/deploy_platform.yml --tags gitlab -e ...
ansible-playbook playbooks/deploy_platform.yml --tags databases -e ...
```
