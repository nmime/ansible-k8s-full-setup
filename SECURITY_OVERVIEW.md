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
  Apps (opwerf, brocoders)
    │
  Platform Services
  (Vault, MinIO, PG, ES, Temporal...)
```

## Security Coverage — 100% Complete

### 1. Pod Security Admission (PSA) — ALL 17 namespaces
| Namespace | Level | Notes |
|-----------|-------|-------|
| production / app_namespace | baseline enforce | App workloads |
| gitlab | baseline enforce | GitLab CE |
| argocd | baseline enforce | GitOps |
| vault | baseline enforce | Secrets management |
| storage | baseline enforce | MinIO |
| databases | baseline enforce | PostgreSQL, MongoDB |
| monitoring | baseline enforce | VictoriaMetrics, Grafana |
| keda | baseline enforce | Autoscaling |
| temporal | baseline enforce | Workflow engine |
| opwerf | baseline enforce | Platform UI |
| elasticsearch | privileged enforce | ELK (needs host access) |
| cilium-system | privileged enforce | CNI (kernel-level) |
| cilium-secrets | baseline enforce | TLS cert storage |
| eso_ns | baseline enforce | External Secrets |
| filebeat / elk | monitoring ns | Co-located |
| postal | baseline enforce | Email MTA |
| gateway | baseline enforce | Ingress gateway |

### 2. NetworkPolicies — 47 CiliumNetworkPolicies
Every namespace has:
- `default-deny` — block all traffic by default
- Scoped ingress rules — only required ports from required sources
- Scoped egress rules — K8s API + explicit cross-namespace deps

| Role | Policies |
|------|----------|
| k8s-cluster-management | 10 (cilium, gateway, cert-manager) |
| opwerf-deployment | 5 (frontend, api, ingress) |
| k8s-secrets (Vault) | 3 |
| k8s-gitops (ArgoCD) | 3 |
| k8s-databases | 3 |
| k8s-observability | 3 |
| gitlab-selfhosted | 4 |
| temporal | 3 |
| brocoders-boilerplate | 4 |
| postal | 3 |
| minio-storage | 2 |
| k8s-autoscaling (KEDA) | 2 |
| elasticsearch | 1 |
| dragonfly | 1 |
| **Total** | **47** |

### 3. ServiceMonitors — All Critical Services
| Service | Namespace | Metrics |
|---------|-----------|----------|
| GitLab | gitlab | /-/metrics |
| ArgoCD | argocd | /metrics |
| PostgreSQL (CNPG) | databases | /metrics |
| KEDA | keda | /metrics |
| OpenWerf API | opwerf | /metrics |
| VictoriaMetrics | monitoring | built-in |
| Vault | vault | /v1/sys/metrics |
| MinIO | storage | /minio/health/live |
| Elasticsearch | elasticsearch | /_prometheus/metrics |
| Temporal | temporal | /metrics |
| Bastion node-exporter | monitoring | :9100 |
| Edge proxies (EU/US/APAC) | monitoring | :9100 |

### 4. Certificate Management
- **cert-manager** — automatic TLS for all ingress routes
- **ClusterIssuer: internal-ca** — internal TLS for Vault, MinIO, Loki, Tempo
- **Let's Encrypt** — edge proxy TLS (auto-renewed weekly via certbot)
- **PrometheusRule: cert-expiry-alerts** — fires 30 days before expiry

### 5. HIPAA Compliance (ON by default)
Set `hipaa_compliance: false` in group_vars to disable.

- Internal TLS (mTLS) for Vault, MinIO, Loki, Tempo
- PII log redaction (SSN, phone, email patterns)
- Audit logging: Vault audit, ES audit, K8s audit → Elasticsearch
- Kubernetes secrets encrypted at rest (AES-CBC)
- Trivy image scanning in CI
- SSH MFA (TOTP)

### 6. Host-Level Security
- **auditd** — on bastion + all K8s nodes
- **unattended-upgrades** — auto security patches
- **node-exporter** — all hosts monitored
- **UFW firewall** — edge proxy servers
- **fail2ban** — SSH + Nginx brute force protection

### 7. ServiceAccount Security
- All app ServiceAccounts: `automountServiceAccountToken: false`
- `external-secrets-vault` SA: `automountServiceAccountToken: false`
- Dedicated SAs per workload (no shared default SA)

## Edge CDN Architecture (roles/edge-cdn)

### Overview
Global edge proxy network with Gcore GeoDNS routing:

```
User request → Gcore DNS (GeoDNS)
  EU user    → EU edge (Hetzner fsn1)
  NA/SA user → US edge (Hetzner ash)
  APAC user  → APAC edge (Hetzner sin)
  Default    → EU edge (fallback)
  Any edge down → auto-removed by Gcore health check
```

### Components
1. **Hetzner VPS** — 3 edge servers (cx21, Ubuntu 22.04)
2. **Nginx** — reverse proxy with 10GB disk cache per edge
3. **Gcore DNS** — GeoDNS with `type: geodns` filter + health checks
4. **Let's Encrypt** — auto-renewing TLS on each edge
5. **Prometheus** — node-exporter + ServiceMonitor per edge
6. **UFW + fail2ban** — edge server hardening

### Gcore DNS API
- Base URL: `https://api.gcore.com`
- Auth: `Authorization: APIKey <token>`
- Zone: `PUT /dns/v2/zones/{zone}/{fqdn}/A`
- Filters chain: `geodns` → `is_healthy` → `default`
- Health checks: `PUT /dns/v2/zones/{zone}/{fqdn}/A/healthchecks`
- Key variable: `gcore_api_key` (env: `GCORE_API_KEY`)

### Cache Policy
| Content | TTL | Cache-Control |
|---------|-----|---------------|
| Static assets (js/css/images) | 30 days | public, immutable |
| HTML pages | 1 hour | — |
| API responses | no-cache | — |
| Default | 10 min | — |

### Configuration
```yaml
# group_vars/all.yml
gcore_api_key: "{{ lookup('env', 'GCORE_API_KEY') }}"
edge_domain: "example.com"
edge_dns_provider: "gcore"  # gcore | hetzner | hetzner_cname
origin_server_ip: "YOUR_K8S_LB_IP"
edge_cache_size: "10g"

edge_regions:
  eu:
    hetzner_location: fsn1
    server_type: cx21
  us:
    hetzner_location: ash
    server_type: cx21
  apac:
    hetzner_location: sin
    server_type: cx21
```

### Playbook Usage
```bash
# Set API key
export GCORE_API_KEY="your_gcore_api_key"

# Deploy edge CDN
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "domain=example.com origin_server_ip=1.2.3.4"

# Purge cache on all edges
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  --tags purge
```

## Alerting Rules Summary

| Alert | Condition | Severity |
|-------|-----------|----------|
| CertExpiryWarning | cert < 30d | warning |
| CertExpiryCritical | cert < 7d | critical |
| EdgeProxyDown | edge up==0 for 2m | critical |
| EdgeProxyHighLatency | p99 > 2s for 5m | warning |
| EdgeCacheHitRateLow | hit rate < 50% for 15m | warning |

## Required Environment Variables
```bash
HCLOUD_TOKEN=         # Hetzner Cloud (server provisioning)
GITHUB_TOKEN=         # GitHub (push)
GCORE_API_KEY=        # Gcore DNS (GeoDNS)
VAULT_ROOT_TOKEN=     # HashiCorp Vault (bootstrap)
```

## Commit History
```
1fe3e0c Complete security hardening: all namespaces PSA+NP+SM coverage
2afb66c Add ServiceMonitors: GitLab, ArgoCD, PostgreSQL, KEDA, OpenWerf API
b235e7f Add NetworkPolicies to all namespaces (7 remaining)
03a7472 Fix OpenWerf: rename dashboard to frontend
8680d98 Secure OpenWerf + Temporal: NetworkPolicies, ServiceAccounts
d9f4236 Secure apps: NetworkPolicies, ServiceAccounts, conditional TLS
52f1108 Add host-level security: auditd, unattended-upgrades, bastion node-exporter
8068836 Add ServiceMonitors, cert expiry alerts, and internal CA for HIPAA
3684b4d Security: enable hipaa_compliance by default
74c0054 Add HIPAA hardening role with internal TLS and log redaction
639a51d Security hardening: fix all audit gaps across all tiers
```
