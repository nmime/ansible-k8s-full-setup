# Security & Infrastructure Overview

## Platform Architecture

```
           Internet
               │
    ┌──────────┼──────────┐
    ▼          ▼          ▼
 Gcore GeoDNS (edge.domain.com)
    │          │          │
  EU Edge    US Edge  APAC Edge
 (Nginx CDN proxy + caching)
    └──────────┼──────────┘
               │ origin
         K8s Cluster
    ┌──────────┴──────────┐
  Gateway (Cilium)       VPN
    │
  Apps (daytona)
    │
  Platform Services
  (Vault, SeaweedFS object storage, PG, ES, Temporal...)
```

## Security Coverage

### 1. Pod Security Admission

All application and platform namespaces are assigned Pod Security Admission labels. App workloads use baseline enforcement, while system components that require kernel-level access use privileged enforcement.

| Namespace | Level | Notes |
|-----------|-------|-------|
| production / app_namespace | baseline enforce | App workloads |
| daytona | baseline enforce | Workspace platform |
| gitlab | baseline enforce | GitLab CE |
| argocd | baseline enforce | GitOps |
| vault | baseline enforce | Secrets management |
| storage | baseline enforce | SeaweedFS object storage |
| databases | baseline enforce | PostgreSQL, MongoDB |
| monitoring | baseline enforce | VictoriaMetrics, Grafana |
| keda | baseline enforce | Autoscaling |
| temporal | baseline enforce | Workflow engine |
| elasticsearch | privileged enforce | ELK needs host access |
| cilium-system | privileged enforce | CNI kernel-level components |
| cilium-secrets | baseline enforce | TLS cert storage |
| eso_ns | baseline enforce | External Secrets |
| filebeat / elk | monitoring ns | Co-located logging components |
| postal | baseline enforce | Email MTA |
| gateway | baseline enforce | Ingress gateway |

### 2. Network Policies

Every namespace is protected by default-deny behavior plus explicit ingress and egress rules for required service-to-service traffic.

| Role | Policies |
|------|----------|
| k8s-cluster-management | Cilium, Gateway, cert-manager |
| daytona-deployment | Frontend, API, ingress |
| k8s-secrets | Vault |
| k8s-gitops | ArgoCD |
| k8s-databases | PostgreSQL and MongoDB |
| k8s-observability | Monitoring and logging |
| gitlab-selfhosted | GitLab services |
| temporal | Temporal server and Web UI |
| postal | Mail services |
| object-storage-storage | Object storage |
| k8s-autoscaling | KEDA |
| elasticsearch | Elasticsearch and Kibana |
| dragonfly | Redis v6-compatible datastore |

### 3. Service Monitoring

Critical platform services expose metrics through ServiceMonitor or equivalent observability resources, including GitLab, ArgoCD, PostgreSQL, KEDA, Daytona, VictoriaMetrics, Vault, SeaweedFS object storage, Elasticsearch, Temporal, bastion node-exporter, and edge proxies.

### 4. Certificate Management

- cert-manager issues and renews ingress TLS certificates.
- Internal CA issuers support service-to-service TLS where enabled.
- Let's Encrypt certificates are used for edge proxy TLS.
- Certificate expiry alerts warn before renewal deadlines.

### 5. HIPAA-Oriented Controls

When enabled, the stack supports internal TLS, PII log redaction, Vault audit logging, Kubernetes audit logging, image scanning, secret encryption at rest, and hardened SSH access.

### 6. Host-Level Security

Bastion, edge, and node hosts use auditd, unattended security upgrades, node-exporter, firewall rules, and brute-force protection.

### 7. ServiceAccount Security

Workloads use dedicated ServiceAccounts where practical, and app ServiceAccounts disable automatic token mounting unless the workload requires Kubernetes API access.

## Edge CDN Architecture

The edge CDN role provisions a global Nginx reverse-proxy cache with GeoDNS routing and health checks. User traffic is routed to the nearest healthy edge, and cache misses are proxied back to the Kubernetes origin.

### Components

1. Hetzner VPS edge servers
2. Nginx reverse proxy with disk cache
3. Gcore GeoDNS with health checks
4. Let's Encrypt TLS certificates
5. Prometheus/node-exporter monitoring
6. UFW and fail2ban hardening

### Cache Policy

| Content | TTL | Cache-Control |
|---------|-----|---------------|
| Static assets | 30 days | public, immutable |
| HTML pages | 1 hour | default |
| API responses | no-cache | no cache |
| Default | 10 min | default |

## Required Environment Variables

```bash
HCLOUD_TOKEN=         # Hetzner Cloud provisioning
GITHUB_TOKEN=         # GitHub access
GCORE_API_KEY=        # Gcore DNS
VAULT_ROOT_TOKEN=     # Vault bootstrap
```

## Operational Alerts

| Alert | Condition | Severity |
|-------|-----------|----------|
| CertExpiryWarning | cert < 30d | warning |
| CertExpiryCritical | cert < 7d | critical |
| EdgeProxyDown | edge up == 0 for 2m | critical |
| EdgeProxyHighLatency | p99 > 2s for 5m | warning |
| EdgeCacheHitRateLow | hit rate < 50% for 15m | warning |
