# Roles Directory

This directory contains Ansible roles for each platform component.

| Role | Description | Key Technologies | Version |
|------|-------------|------------------|---------|
| `generate-secrets/` | Centralized credential generation and persistence | Ansible lookups | - |
| `hetzner-infra/` | Cloud infrastructure provisioning | hcloud CLI, Ubuntu 24.04 | - |
| `network-security/` | VPN and bastion hardening | Headscale, Tailscale, UFW, fail2ban | Headscale v0.28.0 |
| `k8s-cluster-management/` | Kubernetes cluster installation | Kubespray, Cilium, Gateway API, cert-manager, MetalLB, Hetzner CCM/CSI | K8s v1.35.4, Kubespray 2.31, Cilium v1.19.4, cert-manager v1.20.2 |
| `k8s-secrets/` | Secrets management | Vault (HA Raft), External Secrets Operator | Vault Chart 0.32.0, ESO Chart 2.5.0 |
| `object-storage/` | S3-compatible object storage | SeaweedFS official Helm chart | Chart 4.25.1 |
| `k8s-databases/` | Database deployments | Percona PG Operator, Percona MongoDB Operator | PostgreSQL 18, MongoDB 8.0 |
| `gitlab-selfhosted/` | GitLab CE | Helm, GitLab Runner | CE v18.11.3, Chart 9.11.4 |
| `k8s-gitops/` | GitOps continuous delivery | ArgoCD, ApplicationSets | Chart 9.5.14 |
| `k8s-observability/` | Monitoring, logging, dashboards, alerting | VictoriaMetrics, Loki, Grafana, PMM | - |
| `k8s-autoscaling/` | Event-driven autoscaling | KEDA | Chart 2.19.0 |
| `temporal/` | Workflow orchestration engine | Temporal Server, Web UI | Chart 1.2.0 |
| `dragonfly/` | Redis-compatible in-memory store | Dragonfly Operator, Dragonfly CRD | Operator v1.5.0, Dragonfly v1.38.1 |
| `postal/` | Mail server | Postal, MariaDB, Dragonfly | v3.3.6 |
| `glitchtip/` | Sentry-compatible error tracking | GlitchTip Helm chart, PostgreSQL, Dragonfly | App v6.1.4, Chart 8.2.0 |
| `apm-server/` | Distributed tracing ingest | Elastic APM / OTLP, Elasticsearch | APM Server 9.4.1 |
| `blackbox-exporter/` | Uptime and synthetic probes | prometheus-blackbox-exporter, VMProbe | Chart 11.10.0 |
| `daytona-deployment/` | Optional workspace platform | Daytona Helm chart | Chart 0.0.23 |

Object storage note: RustFS was evaluated as an Apache-2.0 future/alternative backend; SeaweedFS remains the selected backend because RustFS Helm/app are beta and SeaweedFS has a mature chart plus live validation.

## Resource and Version Notes

- `medium` and `production` profiles now size both control planes and workers on
  16Gi-class Hetzner `cx43` nodes; update cost expectations with current Hetzner
  pricing rather than treating repository estimates as guarantees.
- Medium profile storage is sized for 4x100Gi object storage replicas, 50Gi
  PostgreSQL, 20Gi Vault, and 100Gi metrics retention.
- Production profile storage is sized for 4x150Gi object storage replicas, 100Gi
  PostgreSQL, 20Gi Vault, and 150Gi metrics retention.
- GlitchTip and APM run two replicas on medium/production tiers with bumped CPU/RAM
  requests; Blackbox keeps a small footprint but uses chart 11.10.0 and 60s probes.

## Deployment Order

Roles are deployed sequentially to respect dependencies:

```
 1. generate-secrets        Generate all platform credentials (idempotent)
 2. hetzner-infra           Provision VPC, subnets, firewalls, bastion, nodes, LB, DNS
 3. network-security        Bastion hardening, NAT gateway, Headscale VPN
 4. k8s-cluster-management  Kubespray K8s install, Cilium, Gateway API, cert-manager, CCM/CSI
 5. k8s-secrets             Vault, auto-init/unseal, ESO + ClusterSecretStore
 6. object-storage           SeaweedFS S3, pre-created buckets
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
