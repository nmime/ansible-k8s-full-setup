# Ansible Kubernetes Full-Stack Platform

> Production-grade Kubernetes platform on Hetzner Cloud — from bare metal to running applications in a single command. Fully automated, security-hardened, HIPAA-ready, with global edge CDN.

[![Security](https://img.shields.io/badge/security-100%25%20coverage-brightgreen)](#security--compliance)
[![Tested](https://img.shields.io/badge/tested-4%2F4%20tiers%20passed-success)](#deployment-tiers)
[![Kubernetes](https://img.shields.io/badge/kubernetes-v1.34.3-326ce5?logo=kubernetes&logoColor=white)](#tech-stack)
[![HIPAA](https://img.shields.io/badge/HIPAA-ready%20by%20default-blue)](#hipaa-compliance)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#license)

---

## What This Is

A **complete, production-ready Kubernetes platform** deployed via a single Ansible playbook:

- ✅ **Zero-to-production in 3-5 hours** (fully automated)
- ✅ **19 integrated roles**: infrastructure → K8s → databases → CI/CD → monitoring → edge CDN
- ✅ **100% security coverage**: NetworkPolicies everywhere, PSA everywhere, HIPAA-ready
- ✅ **Global edge network**: GeoDNS with EU/US/APAC edge proxies (Gcore DNS)
- ✅ **4 deployment tiers**: from €16/mo dev to €97/mo production HA
- ✅ **Private-only architecture**: nodes behind bastion + VPN, no public IPs
- ✅ **Idempotent**: safe to re-run at any point

---

## Table of Contents

- [Architecture](#architecture)
  - [Infrastructure Architecture](#infrastructure-architecture)
  - [Edge CDN Architecture](#edge-cdn-architecture)
  - [Traffic Flow](#traffic-flow)
  - [Security Layers](#security-layers)
- [Tech Stack](#tech-stack)
- [Deployment Tiers](#deployment-tiers)
- [Security & Compliance](#security--compliance)
- [Edge CDN & GeoDNS](#edge-cdn--geodns)
- [Quick Start](#quick-start)
- [How It Works](#how-it-works)
- [Monitoring & Observability](#monitoring--observability)
- [Databases](#databases)
- [Backup & Restore](#backup--restore)
- [DNS Configuration](#dns-configuration)
- [Troubleshooting](#troubleshooting)

---

## Architecture

### Infrastructure Architecture

```
                                  INTERNET
                                     │
             ┌───────────────────────┼───────────────────────┐
             │                       │                       │
       Gcore GeoDNS           Hetzner Load Balancer   Direct VPN
    (edge.domain.com)           (domain.com)        (vpn.domain.com)
             │                       │                       │
      ┌──────┴──────┐          ┌────┴────┐           ┌──────┴──────┐
   EU Edge   US Edge      Public HTTP/S          Headscale VPN
   (fsn1)    (ash)           Gateway               (WireGuard)
      │         │                 │                       │
      └─────────┴─────────────────┼───────────────────────┘
                                  │
                    ┌─────────────┴─────────────┐
                    │     HETZNER CLOUD         │
                    │   Private Network         │
                    │    10.0.0.0/16           │
                    │                           │
        ┌───────────┼───────────────────────────┼───────────┐
        │  Bastion  │   Control Plane (HA)     │  Workers  │
        │ (NAT GW)  │   10.0.1.0/24            │ 10.0.2.0/24│
        │           │   ┌────┬────┬────┐       │           │
        │• fail2ban │   │CP1 │CP2 │CP3 │       │  ┌───┬────┤
        │• UFW      │   └────┴────┴────┘       │  │W1 │W2  │
        │• auditd   │                          │  └───┴────┤
        │• VPN      │   • etcd (HA Raft)       │           │
        └───────────┤   • kube-apiserver (3x)  │  • Apps   │
                    │   • kube-scheduler       │  • DBs    │
                    │   • kube-controller-mgr  │  • CI/CD  │
                    └──────────────────────────┴───────────┘
```

### Edge CDN Architecture

```
                         User Request
                              │
                    ┌─────────┴──────────┐
                    │   Gcore DNS        │
                    │   GeoDNS Filter    │
                    └─────────┬──────────┘
                              │
          ┌───────────────────┼───────────────────┐
          │                   │                   │
       EU User             US User            APAC User
          │                   │                   │
          ▼                   ▼                   ▼
    ┌──────────┐        ┌──────────┐        ┌──────────┐
    │ EU Edge  │        │ US Edge  │        │APAC Edge │
    │ (fsn1)   │        │ (ash)    │        │ (sin)    │
    │          │        │          │        │          │
    │ Nginx    │        │ Nginx    │        │ Nginx    │
    │ 10GB     │        │ 10GB     │        │ 10GB     │
    │ cache    │        │ cache    │        │ cache    │
    │          │        │          │        │          │
    │ • UFW    │        │ • UFW    │        │ • UFW    │
    │ • TLS    │        │ • TLS    │        │ • TLS    │
    │• fail2ban│        │• fail2ban│        │• fail2ban│
    └────┬─────┘        └────┬─────┘        └────┬─────┘
         │                   │                   │
         └───────────────────┼───────────────────┘
                             │
                    origin.domain.com
                             │
                   ┌─────────┴─────────┐
                   │  K8s Cluster      │
                   │  (Hetzner Cloud)  │
                   └───────────────────┘

Health Checks: Gcore monitors /health every 30s
               Failed edges auto-removed from DNS
Cache TTL:     Static assets: 30 days
               HTML: 1 hour
               Default: 10 min
```

### Traffic Flow

#### Public Traffic (User-facing apps)

```
Internet
  → Gcore GeoDNS (continent-based routing)
    → Nearest Edge Proxy (EU/US/APAC)
      → Cache HIT: serve from edge (30d for static, 1h for HTML)
      → Cache MISS: proxy to origin
        → Hetzner LB :80/:443
          → TCP passthrough to NodePort :30080/:30443 (workers)
            → Cilium Gateway (TLS termination)
              → HTTPRoute (host/path matching)
                → Backend Service
                  → Pod
```

#### Admin Traffic (VPN-only: GitLab, Grafana, ArgoCD, Vault)

```
VPN Client (100.64.0.0/10)
  → Headscale VPN (WireGuard)
    → admin-gateway NodePort :31443
      → Cilium Gateway (TLS termination)
        → HTTPRoute
          → Admin Service
            → Pod

NetworkPolicy: CiliumNetworkPolicy restricts admin-gateway
               to VPN (100.64.0.0/10) + private (10.0.0.0/16) only
```

#### Internal Service-to-Service

```
Pod A (namespace: production)
  → ClusterIP service
    → CiliumNetworkPolicy checks:
        ✓ Egress allowed from production to databases ns?
        ✓ Ingress allowed to databases from production ns?
      → Pod B (namespace: databases)
```

### Security Layers

```
┌─────────────────────────────────────────────────────────┐
│ Layer 1: Network Perimeter                              │
│  • Hetzner Firewalls (fw-bastion, fw-nodes)            │
│  • Bastion: UFW (SSH, HTTPS, WireGuard only)           │
│  • Nodes: All traffic private/VPN only                  │
│  • fail2ban on bastion + edge proxies                   │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 2: VPN                                            │
│  • Headscale (self-hosted Tailscale)                    │
│  • 100.64.0.0/10 overlay network                        │
│  • Admin access: GitLab, Grafana, ArgoCD, Vault, PMM   │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 3: Network Policies (Cilium)                      │
│  • 47 CiliumNetworkPolicies                             │
│  • default-deny in every namespace                      │
│  • Scoped ingress/egress (least privilege)              │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 4: Pod Security Admission                         │
│  • 17 namespaces with PSA labels                        │
│  • baseline enforce (restricted warn) by default        │
│  • privileged only for CNI/ES (kernel access)           │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 5: ServiceAccount Security                        │
│  • automountServiceAccountToken: false                  │
│  • Dedicated SAs per workload                           │
│  • No shared default SA usage                           │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 6: Secrets Management                             │
│  • HashiCorp Vault (HA Raft)                           │
│  • K8s secrets encrypted at rest (AES-CBC)             │
│  • External Secrets Operator (K8s ↔ Vault sync)        │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 7: TLS Everywhere                                 │
│  • cert-manager (Let's Encrypt)                         │
│  • Internal CA for inter-service mTLS (HIPAA)           │
│  • Auto-renewed (30d before expiry)                     │
│  • PrometheusRule alerts on expiry                      │
└─────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────┐
│ Layer 8: Audit Logging (HIPAA)                          │
│  • Vault audit logs → Elasticsearch                     │
│  • K8s audit logs → Elasticsearch                       │
│  • ES audit logs (own audit trail)                      │
│  • auditd on all hosts → syslog → Loki                 │
└─────────────────────────────────────────────────────────┘
```

---

## Tech Stack

### Core Platform (Kubernetes)

| Component | Version | Purpose |
|-----------|---------|--------|
| **Kubernetes** | `v1.34.3` | Container orchestration (Kubespray `v2.30.0`) |
| **Cilium** | `v1.19.2` | eBPF CNI, NetworkPolicies, Hubble observability |
| **Gateway API** | `v1.5.1` | L7 ingress (replaces Ingress) |
| **cert-manager** | `v1.20.1` | Automated TLS (Let's Encrypt + internal CA) |
| **MetalLB** | `v0.15.3` | Bare-metal LB (L2 mode, private VIPs) |
| **Hetzner CCM** | `v1.30.1` | Cloud controller manager |
| **Hetzner CSI** | `v2.20.0` | Persistent volume provisioning |

### Platform Services

| Component | Version | Purpose |
|-----------|---------|--------|
| **HashiCorp Vault** | `v1.21.2` | Secrets management, HA Raft, auto-unseal |
| **External Secrets Operator** | `v0.15.0` | K8s ↔ Vault secret sync |
| **MinIO** | chart `5.4.0` | S3-compatible object storage (distributed mode) |
| **PostgreSQL 18** | Percona Operator `2.8.2` | HA PostgreSQL + PgBouncer + pgBackRest |
| **MongoDB 8.0** | Percona Operator `1.22.0` | Replicated MongoDB + PBM backups |
| **GitLab CE** | `v18.10.0` | Git, CI/CD, Container Registry, KAS |
| **ArgoCD** | `v3.3.6` | GitOps continuous delivery |
| **VictoriaMetrics** | `v1.133.0` | Metrics (faster than Prometheus) |
| **Grafana** | `v12.3.1` | Dashboards (12 pre-built) |
| **Loki** | `v3.6.3` | Log aggregation |
| **Promtail** | chart `6.17.1` | Log shipping |
| **PMM Server** | `v3` | Percona Monitoring & Management |
| **Dragonfly** | `v1.37.2` | Redis-compatible (25x faster) |
| **KEDA** | `v2.18.2` | Event-driven autoscaling |
| **Temporal** | `v1.29.1` | Workflow orchestration |
| **Headscale** | `v0.28.0` | Self-hosted WireGuard VPN |
| **Elasticsearch** | `v8.x` | ELK stack for audit logs |
| **Filebeat** | latest | K8s audit log shipping |

### Edge CDN Stack

| Component | Version | Purpose |
|-----------|---------|--------|
| **Hetzner VPS** | Ubuntu 22.04 | Edge proxy servers (cx21) |
| **Nginx** | latest | Reverse proxy + 10GB cache |
| **Let's Encrypt** | via certbot | Edge TLS (auto-renewed weekly) |
| **Gcore DNS** | API v2 | GeoDNS with health checks |
| **UFW + fail2ban** | latest | Edge security hardening |
| **node-exporter** | latest | Edge server monitoring |

---

## Deployment Tiers

### Tier Comparison

| | **Minimal** | **Small** | **Medium** | **Production** |
|---|---|---|---|---|
| **Monthly Cost** | ~€16 | ~€40 | ~€52 | ~€97 |
| **Best For** | Dev / Learning | Startups / Staging | Small teams | Production workloads |
| **Deploy Time** | ~4 hours | ~1.5 hours | ~5.5 hours | ~5.5 hours |
| **Nodes Total** | 2 | 3 | 5 | 6 |
| **Control Plane** | 1× cx22 (2c/4GB) | 1× cx22 (2c/4GB) | 3× cx22 (HA) | 3× cpx31 (HA, 4c/8GB) |
| **Workers** | 1× cx32 (2c/8GB) | 2× cpx31 (4c/8GB) | 2× cpx31 | 3× cpx31 |
| **CP Schedulable** | ✅ Yes | ✅ Yes | ✅ Yes | ❌ No (HA best practice) |
| **Load Balancer** | ❌ (bastion proxy) | ✅ lb11 | ✅ lb11 | ✅ lb11 |
| **Placement Group** | ❌ | ❌ | ✅ spread | ✅ spread |
| **HA Components** | None | None | Vault, MinIO | Vault, MinIO, PG, Mongo |

### Component Scaling by Tier

| Component | Minimal | Small | Medium | Production |
|-----------|---------|-------|--------|------------|
| **Vault** | 1 standalone | 1 standalone | 3 HA Raft | 3 HA Raft |
| **MinIO** | 1 (50Gi) | 1 (100Gi) | 4 distributed (50Gi×4) | 4 distributed (100Gi×4) |
| **PostgreSQL** | 1 (10Gi) | 1 (20Gi) | 2 replicas (20Gi) | 3 replicas (50Gi) |
| **MongoDB** | 1 | 1 | 3 replicas | 3 replicas |
| **VictoriaMetrics** | VMSingle (10Gi) | VMSingle (20Gi) | VMCluster (50Gi) | VMCluster (100Gi) |
| **Loki** | SingleBinary | SingleBinary | SimpleScalable | SimpleScalable |
| **Metrics Retention** | 7 days | 14 days | 30 days | 30 days |
| **Log Retention** | 7 days | 14 days | 30 days | 30 days |
| **Backups** | ❌ | ❌ | ✅ Daily | ✅ Daily |

---

## Security & Compliance

### Security Coverage — 100% Complete ✅

#### 1. Pod Security Admission (PSA) — 17 Namespaces

Every namespace has PSA labels:

```yaml
metadata:
  labels:
    pod-security.kubernetes.io/enforce: baseline  # or privileged
    pod-security.kubernetes.io/warn: restricted
```

| Namespace | Level | Why |
|-----------|-------|-----|
| production, opwerf, temporal, databases, storage, vault, argocd, gitlab, keda, monitoring, eso, postal, gateway, cilium-secrets | **baseline enforce** | Standard workloads |
| elasticsearch, cilium-system | **privileged enforce** | Needs host/kernel access |

#### 2. NetworkPolicies — 47 CiliumNetworkPolicies

Every namespace has:
- **default-deny**: blocks all traffic by default
- **Scoped ingress**: only required ports from required sources
- **Scoped egress**: K8s API + explicit cross-namespace deps

| Role | Policies | Coverage |
|------|----------|----------|
| k8s-cluster-management | 10 | Cilium, Gateway, cert-manager |
| opwerf-deployment | 5 | frontend, API, worker |
| gitlab-selfhosted | 4 | webservice, registry, KAS |
| brocoders-boilerplate | 4 | frontend, backend |
| k8s-secrets (Vault) | 3 | Vault pods, ESO access |
| k8s-gitops (ArgoCD) | 3 | server, repo-server, controller |
| k8s-databases | 3 | PG, Mongo, PgBouncer |
| k8s-observability | 3 | VMAgent, Promtail, Grafana |
| temporal | 3 | frontend, worker, history |
| postal | 3 | SMTP relay, web console |
| minio-storage | 2 | API, Console |
| k8s-autoscaling (KEDA) | 2 | operator, webhook |
| elasticsearch | 1 | ES cluster |
| dragonfly | 1 | Redis-compatible |
| **Total** | **47** | **100% namespace coverage** |

#### 3. ServiceMonitors — 12 Total

All critical services monitored by VictoriaMetrics:

| Service | Namespace | Endpoint |
|---------|-----------|----------|
| GitLab | gitlab | `/-/metrics` |
| ArgoCD | argocd | `/metrics` |
| PostgreSQL (CNPG) | databases | `/metrics` (9187) |
| KEDA | keda | `/metrics` |
| OpenWerf API | opwerf | `/metrics` |
| Vault | vault | `/v1/sys/metrics` |
| MinIO | storage | `/minio/health/live` |
| Elasticsearch | elasticsearch | `/_prometheus/metrics` |
| Temporal | temporal | `/metrics` |
| Bastion | monitoring | `:9100` (node-exporter) |
| Edge EU | monitoring | `:9100` (via Endpoints) |
| Edge US/APAC | monitoring | `:9100` (via Endpoints) |

#### 4. Certificate Management

- **cert-manager**: automatic TLS for all HTTPRoutes (Let's Encrypt DNS01)
- **ClusterIssuer: internal-ca**: 1-year certs for internal mTLS (Vault, MinIO, Loki, Tempo)
- **PrometheusRule**: `CertExpiryWarning` (30d), `CertExpiryCritical` (7d)
- **Edge proxies**: Let's Encrypt via certbot (auto-renewed weekly)

#### 5. ServiceAccount Security

- All app SAs: `automountServiceAccountToken: false`
- Dedicated SA per workload (no shared default SA)
- Examples:
  - `opwerf-frontend-sa`
  - `opwerf-api-sa`
  - `external-secrets-vault`

#### 6. HIPAA Compliance (ON by default)

Set `hipaa_compliance: false` to disable.

**Features:**
- **Internal TLS (mTLS)**: Vault, MinIO, Loki, Tempo use internal CA certs
- **PII log redaction**: SSN, phone, email patterns stripped from logs
- **Audit logging**:
  - Vault audit logs → Elasticsearch
  - K8s audit logs → Elasticsearch (via Filebeat)
  - ES audit logs (self-auditing)
  - Host auditd → Loki
- **Secrets encrypted at rest**: K8s secrets use AES-CBC encryption
- **Image scanning**: Trivy in CI pipeline (configurable)
- **SSH MFA**: TOTP two-factor auth on bastion

**Role**: `roles/hipaa-hardening/`

#### 7. Host-Level Security

**All hosts:**
- **auditd**: logs all syscalls → Loki
- **unattended-upgrades**: auto security patches
- **node-exporter**: metrics for monitoring

**Bastion:**
- **UFW**: SSH(22), HTTPS(443), WireGuard(41641) only
- **fail2ban**: SSH brute-force protection
- **SSH hardening**:
  - PasswordAuthentication no
  - PermitRootLogin prohibit-password
  - TOTP MFA (optional, HIPAA)

**Edge proxies:**
- **UFW**: HTTP(80), HTTPS(443), SSH(22), node-exporter(9100)
- **fail2ban**: SSH + Nginx protection

#### 8. Network Segmentation

- **10.0.0.0/16**: private network (Hetzner)
- **10.0.1.0/24**: control plane
- **10.0.2.0/24**: workers
- **10.0.10.0/24**: MetalLB VIPs
- **100.64.0.0/10**: VPN overlay (Headscale)

**Firewalls:**
- `fw-bastion`: public SSH/HTTPS/WireGuard only
- `fw-nodes`: all traffic restricted to private + VPN ranges

---

## Edge CDN & GeoDNS

### Overview

**roles/edge-cdn** provisions a global CDN with 3 edge proxies:
- **EU**: Hetzner fsn1 (Falkenstein, Germany)
- **US**: Hetzner ash (Ashburn, Virginia)
- **APAC**: Hetzner sin (Singapore)

**Gcore DNS** routes users to nearest edge based on continent.

### How It Works

```
1. User visits app.example.com
2. DNS query → Gcore DNS API
3. Gcore GeoDNS filter checks user location:
   - EU user → returns EU edge IP (49.12.x.x)
   - US user → returns US edge IP (142.132.x.x)
   - APAC user → returns APAC edge IP (138.201.x.x)
   - Default → returns EU edge IP (fallback)
4. User connects to nearest edge
5. Edge Nginx checks cache:
   - HIT: serve from 10GB local cache
   - MISS: proxy to origin.example.com (K8s LB)
6. Origin serves response
7. Edge caches response (TTL based on content type)
8. Edge returns response to user
```

### Gcore DNS API Integration

**Authentication:**
```bash
Authorization: APIKey <your_gcore_api_key>
```

**Base URL:**
```
https://api.gcore.com
```

**Key endpoints:**

| Method | Endpoint | Purpose |
|--------|----------|--------|
| POST | `/dns/v2/zones` | Create DNS zone |
| GET | `/dns/v2/zones/{zone}` | Get zone details |
| PUT | `/dns/v2/zones/{zone}/{fqdn}/A` | Create/update A record with GeoDNS |
| PUT | `/dns/v2/zones/{zone}/{fqdn}/A/healthchecks` | Configure health checks |

**GeoDNS A record structure:**

```json
{
  "ttl": 300,
  "filters": [
    {"type": "geodns"},
    {"type": "is_healthy", "strict": false},
    {"type": "default", "limit": 1}
  ],
  "resource_records": [
    {
      "content": ["49.12.245.85"],
      "meta": {
        "continents": ["EU"],
        "default": false
      }
    },
    {
      "content": ["142.132.201.67"],
      "meta": {
        "continents": ["NA", "SA"],
        "default": false
      }
    },
    {
      "content": ["138.201.104.22"],
      "meta": {
        "continents": ["AS", "OC"],
        "default": false
      }
    },
    {
      "content": ["49.12.245.85"],
      "meta": {"default": true}
    }
  ]
}
```

**Health check structure:**

```json
{
  "frequency": 30,
  "timeout": 10,
  "protocol": "HTTP",
  "port": 443,
  "tls": true,
  "method": "GET",
  "url": "/health",
  "host": "example.com",
  "expected_http_statuses": [200]
}
```

### Edge Proxy Features

**Nginx Configuration:**
- **10GB disk cache** per edge (`proxy_cache_path`)
- **Stale cache serving**: serves stale on origin error
- **Background cache updates**: refreshes cache without blocking user
- **Cache lock**: prevents thundering herd

**Cache TTL:**

| Content Type | TTL | Cache-Control |
|--------------|-----|---------------|
| Static assets (`.jpg`, `.css`, `.js`, `.woff2`) | 30 days | `public, immutable` |
| HTML (`.html`, `.htm`) | 1 hour | — |
| API responses (`/api/*`) | no-cache | — |
| Default | 10 min | — |

**Security Headers:**
```nginx
Strict-Transport-Security: max-age=63072000; includeSubDomains; preload
X-Content-Type-Options: nosniff
X-Frame-Options: DENY
X-Cache-Status: $upstream_cache_status  # HIT/MISS/BYPASS/EXPIRED
X-Edge-Region: EU / US / APAC
```

**TLS:**
- Let's Encrypt wildcard cert via certbot
- Auto-renewed weekly (cron: `0 3 */7 * *`)
- TLS 1.2 + 1.3 only
- Modern cipher suite

**Cache Purge:**
- Weekly CronJob in K8s (Sunday 4 AM)
- Manual: `curl https://<edge-ip>/purge/*`
- Restricted to origin IPs via `allow` directive

### Edge Monitoring

**node-exporter** on each edge:
- Port 9100
- Exported as K8s Endpoints + Service
- ServiceMonitor scrapes all edges

**Alerting rules (PrometheusRule):**

| Alert | Condition | Severity |
|-------|-----------|----------|
| `EdgeProxyDown` | `up{job=~"edge-.*"} == 0` for 2m | critical |
| `EdgeProxyHighLatency` | `nginx_http_request_duration_seconds{quantile="0.99"} > 2` for 5m | warning |
| `EdgeCacheHitRateLow` | Cache hit rate < 50% for 15m | warning |

### Configuration Variables

**Required:**
```yaml
gcore_api_key: "{{ lookup('env', 'GCORE_API_KEY') }}"
edge_domain: "example.com"
origin_server_ip: "116.203.x.x"  # K8s LB or bastion IP
```

**Optional:**
```yaml
edge_subdomain: "cdn"  # cdn.example.com
edge_cache_size: "10g"
edge_cache_path: "/var/cache/nginx"
edge_upstream_host: "origin.{{ domain }}"
edge_health_check_path: "/health"
edge_health_check_interval: 30
edge_tls_cert_email: "admin@example.com"

# Customize regions
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

### Deployment

**1. Set API key:**
```bash
export GCORE_API_KEY="your_gcore_api_key_here"
```

**2. Deploy edge CDN:**
```bash
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "domain=example.com" \
  -e "origin_server_ip=116.203.12.34"
```

**What it does:**
- Provisions 3 Hetzner VPS (EU/US/APAC)
- Installs Nginx + certbot + UFW + fail2ban
- Obtains Let's Encrypt certs
- Configures Nginx caching proxy
- Creates Gcore DNS zone
- Creates GeoDNS A records (app.example.com, cdn.example.com)
- Configures health checks
- Creates K8s ServiceMonitor + PrometheusRule
- Creates cache purge CronJob

**Output:**
```
========================================
EDGE CDN DEPLOYMENT COMPLETE
========================================
Domain: example.com
CDN: cdn.example.com
Origin: origin.example.com -> 116.203.12.34

Edge Servers:
  EU: 49.12.245.85
  US: 142.132.201.67
  APAC: 138.201.104.22

GeoDNS Routing (Gcore):
  EU traffic  -> 49.12.245.85
  NA/SA traffic -> 142.132.201.67
  APAC traffic -> 138.201.104.22
  Default     -> 49.12.245.85

Health Checks: /health every 30s
Auto-failover: enabled (unhealthy servers removed)
TLS: Let's Encrypt (auto-renew weekly)
Cache: 10g per edge
Monitoring: node-exporter + ServiceMonitor
========================================
```

**3. Verify:**
```bash
# Check DNS
dig app.example.com

# From EU
curl -I https://app.example.com
# X-Edge-Region: EU
# X-Cache-Status: MISS (first request)

curl -I https://app.example.com
# X-Cache-Status: HIT (cached)

# Check health
curl https://49.12.245.85/health -H "Host: example.com" --insecure
# {"status":"ok","region":"EU"}
```

---

## Quick Start

### Prerequisites

1. **Hetzner Cloud account**
   - Generate API token: https://console.hetzner.cloud/projects
   - Export: `export HCLOUD_TOKEN="your_token"`

2. **Gcore account** (for edge CDN)
   - Get API key: https://gcore.com/
   - Export: `export GCORE_API_KEY="your_key"`

3. **GitHub token** (for GitLab + ArgoCD)
   - Generate: https://github.com/settings/tokens
   - Export: `export GITHUB_TOKEN="ghp_..."`

4. **Domain with DNS access**
   - Transfer to Hetzner DNS (recommended) or
   - Update NS records to Hetzner DNS servers

5. **Ansible control machine**
   ```bash
   pip3 install ansible==9.x
   ansible-galaxy collection install -r requirements.yml
   ```

### Deploy Platform

**Option 1: Via orchestrator (recommended)**

```bash
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup

# Set environment variables
export HCLOUD_TOKEN="..."
export GITHUB_TOKEN="..."
export GCORE_API_KEY="..."  # optional, for edge CDN

# Deploy
./platform-orchestrator/platform.sh deploy \
  --tier medium \
  --project my-platform \
  --domain example.com
```

**Option 2: Via run script**

```bash
./run_tier.sh medium my-platform example.com
```

**Option 3: Manual Ansible**

```bash
ansible-playbook -i inventory/hosts.yml playbooks/deploy_platform.yml \
  -e "tier=medium" \
  -e "project_name=my-platform" \
  -e "domain=example.com" \
  -e "hcloud_token=$HCLOUD_TOKEN" \
  -e "admin_email=admin@example.com"
```

### Deploy Edge CDN (Optional)

After platform deployment:

```bash
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "domain=example.com" \
  -e "origin_server_ip=$(hcloud load-balancer list | grep lb-my-platform | awk '{print $4}')"
```

### Access Services

**VPN (Headscale):**
```bash
# Get join key
ssh root@vpn.example.com "headscale preauthkeys create --expiration 24h --reusable"

# Install client
sudo apt install tailscale

# Connect
sudo tailscale up --login-server=https://vpn.example.com --authkey=<key>
```

**GitLab:**
- URL: `https://gitlab.example.com` (VPN required)
- Root password: Check Vault or `kubectl get secret -n gitlab gitlab-gitlab-initial-root-password`

**ArgoCD:**
- URL: `https://argocd.example.com` (VPN required)
- Password: `kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath="{.data.password}" | base64 -d`

**Grafana:**
- URL: `https://grafana.example.com` (VPN required)
- Default: admin/admin (change on first login)

**Vault:**
- URL: `https://vault.example.com` (VPN required)
- Root token: In generated secrets file or `kubectl get secret -n vault vault-unseal-keys`

---

## How It Works

### Deployment Flow

```
1. Generate Secrets (roles/generate-secrets)
   - SSH keys
   - Random passwords (DB, GitLab, Grafana)
   - TLS certs for internal CA
   - Vault unseal keys
   └─> Stored in: ./generated-secrets/<project_name>/

2. Provision Infrastructure (roles/hetzner-infra)
   - Create private network (10.0.0.0/16)
   - Create firewalls (fw-bastion, fw-nodes)
   - Create placement group (medium/production)
   - Create bastion (public IP)
   - Create control plane nodes (1 or 3)
   - Create worker nodes (1-3)
   - Create load balancer (small/medium/production)
   - Create DNS A records (*, vpn)
   └─> Outputs: inventory/hosts.yml

3. Harden Bastion (roles/network-security)
   - Install Headscale VPN
   - Configure NAT gateway (iptables MASQUERADE)
   - Install fail2ban
   - Configure UFW (SSH, HTTPS, WireGuard)
   - Setup auditd
   - Install node-exporter
   └─> VPN ready, NAT active

4. Install Kubernetes (via Kubespray)
   - Control plane: etcd + kube-apiserver + scheduler + controller-manager
   - Workers: kubelet + kube-proxy
   - CNI: Calico (replaced by Cilium later)
   └─> K8s cluster ready

5. Install CNI & Core (roles/k8s-cluster-management)
   - Replace Calico with Cilium (eBPF mode)
   - Install Gateway API CRDs
   - Install cert-manager + ClusterIssuer (Let's Encrypt DNS01)
   - Install Hetzner CCM + CSI
   - Create Gateway resources (main-gateway, admin-gateway)
   - Create ReferenceGrants
   - Configure NetworkPolicies (Gateway, cert-manager, Cilium)
   └─> Traffic routing ready

6. Secrets Management (roles/k8s-secrets)
   - Install Vault (HA Raft or standalone)
   - Initialize Vault (auto-unseal)
   - Create KV v2 secret engine
   - Install External Secrets Operator
   - Create SecretStore (Vault backend)
   - Configure Vault Kubernetes auth
   - Create NetworkPolicies (Vault, ESO)
   └─> Secrets ready

7. Object Storage (roles/minio-storage)
   - Deploy MinIO (distributed or standalone)
   - Create buckets: gitlab, backups, temporal, opwerf
   - Store credentials in Vault
   - Create NetworkPolicies (MinIO API, Console)
   └─> S3 storage ready

8. Observability Stack (roles/k8s-observability)
   - Deploy VictoriaMetrics (VMCluster or VMSingle)
   - Deploy Grafana + datasources
   - Deploy Loki (SimpleScalable or SingleBinary)
   - Deploy Promtail (all nodes)
   - Create 12 dashboards
   - Create VMRules (alerting)
   - Deploy Elasticsearch (if HIPAA)
   - Deploy Filebeat (K8s audit logs)
   - Create NetworkPolicies (monitoring ns)
   └─> Metrics + logs ready

9. Databases (roles/k8s-databases)
   - Deploy Percona PostgreSQL Operator
   - Create PerconaPGCluster (HA or standalone)
   - Configure PgBouncer
   - Configure pgBackRest → MinIO
   - Deploy Percona MongoDB Operator
   - Create PSMDB cluster
   - Configure PBM → MinIO
   - Deploy PMM Server
   - Create cross-namespace secrets (GitLab PG, Temporal PG)
   - Create NetworkPolicies (databases ns)
   └─> Databases ready

10. GitLab (roles/gitlab-selfhosted)
    - Deploy GitLab CE Helm chart
    - Configure S3 for artifacts/LFS/uploads/backups
    - Configure PostgreSQL external connection
    - Configure Container Registry
    - Configure GitLab KAS (Agent Server)
    - Deploy GitLab Runner
    - Create HTTPRoutes (GitLab, Registry, KAS) on admin-gateway
    - Create NetworkPolicies (gitlab ns)
    - Create ServiceMonitor
    └─> GitLab ready (https://gitlab.example.com)

11. GitOps (roles/k8s-gitops)
    - Deploy ArgoCD
    - Configure Git repo access (GitHub token)
    - Create ApplicationSet (app-of-apps pattern)
    - Create HTTPRoute on admin-gateway
    - Create NetworkPolicies (argocd ns)
    - Create ServiceMonitor
    └─> ArgoCD ready (https://argocd.example.com)

12. Autoscaling (roles/k8s-autoscaling)
    - Deploy KEDA operator
    - Create ScaledObjects (if tier >= medium)
    - Create NetworkPolicies (keda ns)
    - Create ServiceMonitor
    └─> Event-driven autoscaling ready

13. Workflow Engine (roles/temporal)
    - Deploy Temporal (frontend, history, matching, worker)
    - Configure PostgreSQL backend
    - Create HTTPRoute (Temporal UI) on admin-gateway
    - Create NetworkPolicies (temporal ns)
    - Create ServiceMonitor
    └─> Temporal ready (https://temporal.example.com)

14. Redis (roles/dragonfly)
    - Deploy Dragonfly operator
    - Create Dragonfly instance
    - Create NetworkPolicies
    └─> Redis-compatible cache ready

15. Email (roles/postal - optional)
    - Deploy Postal SMTP server
    - Configure MySQL backend
    - Create NetworkPolicies (SMTP egress)
    └─> Email relay ready

16. Sample Apps (roles/brocoders-boilerplate-setup, roles/opwerf-deployment - optional)
    - Deploy NestJS backend + React frontend
    - Create HTTPRoutes on main-gateway (public)
    - Create NetworkPolicies (app ns)
    - Create ServiceAccounts (automountServiceAccountToken: false)
    - Create ServiceMonitors
    └─> Apps ready (https://app.example.com)

17. HIPAA Hardening (roles/hipaa-hardening - if hipaa_compliance: true)
    - Enable internal TLS for Vault, MinIO, Loki, Tempo
    - Configure PII log redaction (Promtail)
    - Enable Vault audit logging → ES
    - Enable K8s audit logging → ES
    - Configure SSH MFA (TOTP)
    └─> HIPAA compliance ready

18. Edge CDN (roles/edge-cdn - optional)
    - Provision 3 edge VPS (EU/US/APAC)
    - Install Nginx + certbot
    - Configure caching proxy
    - Create Gcore DNS zone
    - Create GeoDNS A records
    - Configure health checks
    - Create ServiceMonitors + alerts
    └─> Global CDN ready
```

### Key Design Patterns

#### 1. Private-Only Nodes

**Problem**: K8s nodes with public IPs are expensive and insecure.

**Solution**:
- All nodes on private network (10.0.0.0/16)
- Bastion NAT gateway for outbound traffic
- Hetzner LB for inbound public traffic (or bastion proxy on minimal)
- VPN for admin access

**Result**:
- €5-6/mo saved per node (no public IP)
- Reduced attack surface
- Centralized egress point for logging/monitoring

#### 2. Gateway API instead of Ingress

**Problem**: Ingress Controller (Nginx/Traefik) requires NodePort + additional resources.

**Solution**:
- Cilium Gateway implementation (built into CNI)
- HTTPRoute for L7 routing
- Fixed NodePorts (30080/30443) → Hetzner LB
- Wildcard cert via cert-manager

**Result**:
- No extra ingress controller deployment
- eBPF-accelerated routing
- Native Cilium NetworkPolicy integration

#### 3. Vault + External Secrets Operator

**Problem**: K8s secrets in etcd are accessible to admins.

**Solution**:
- HashiCorp Vault stores all secrets (KV v2 engine)
- External Secrets Operator syncs Vault → K8s secrets
- Applications read secrets as normal K8s secrets
- Vault audit log tracks all access

**Result**:
- Centralized secret management
- Audit trail for compliance
- Secrets rotated in Vault, auto-synced to K8s

#### 4. NetworkPolicy Everywhere

**Problem**: K8s default is "allow all" — any pod can talk to any pod.

**Solution**:
- `default-deny` CiliumNetworkPolicy in every namespace
- Explicit allow rules for required traffic only
- L3/L4 filtering (IP + port)
- L7 filtering for HTTP (Cilium-specific)

**Result**:
- Zero-trust network model
- Lateral movement blocked
- Easy troubleshooting (logs show policy denials)

#### 5. Idempotent Ansible

**Problem**: Re-running playbooks should not break existing state.

**Solution**:
- All tasks use `state: present` (not `state: latest`)
- Helm: `wait: false` + separate readiness checks
- Retries on all external API calls
- Conditional tasks (e.g., Vault init only if not already done)

**Result**:
- Safe to re-run after failure
- Safe to add new features without destroying existing resources

---

## Monitoring & Observability

### Metrics Stack

**VictoriaMetrics** replaces Prometheus:
- 7x less RAM usage
- 7x less disk space
- Faster query execution
- PromQL-compatible

**Architecture:**

```
node-exporter (all nodes) ──┐
kubelet /metrics ───────────┤
kube-state-metrics ─────────┤
ServiceMonitors (12) ───────┼─→ VMAgent (scraper)
                            │     │
                            │     ▼
                            │   VMInsert (ingest)
                            │     │
                            │     ▼
                            │   VMStorage (TSDB)
                            │     │
                            │     ▼
                            │   VMSelect (query)
                            │     │
                            └─────┴─→ Grafana (dashboards)
                                  │
                                  └─→ VMAlert (alerting)
```

### Logs Stack

**Loki** for log aggregation:

```
Pod logs (stdout/stderr) ──┐
Node syslogs ──────────────┼─→ Promtail (shipper)
Audit logs ────────────────┘     │
                                 ▼
                               Loki (ingest + store)
                                 │
                                 ├─→ Grafana (query/visualize)
                                 └─→ LogQL queries in alerts
```

**HIPAA audit trail:**

```
Vault audit ──┐
K8s audit ────┼─→ Filebeat ──→ Elasticsearch
ES audit ─────┘                     │
                                    └─→ Kibana (if deployed)
```

### Dashboards (12 pre-built)

| Dashboard | Key Metrics |
|-----------|-------------|
| **Node Exporter Full** | CPU usage, load avg, memory, disk I/O, network, filesystem usage |
| **Kubernetes Cluster** | Pod status, namespace resource quotas, deployment health, node capacity |
| **VictoriaMetrics** | Ingestion rate, active series, query latency, storage size |
| **Loki Dashboard** | Log volume (lines/s), error rate, ingestion lag, label cardinality |
| **Cilium Overview** | Network flows, packet drops, policy verdicts, endpoint health |
| **PostgreSQL Overview** | Connections, TPS, locks, buffer hit ratio, replication lag |
| **MongoDB Overview** | Operations/s, connections, replication lag, cache hit ratio |
| **Percona PG Overview** | Percona-specific metrics (PgBouncer, pgBackRest status) |
| **Percona PG Replication** | Replication slots, WAL lag, streaming status |
| **Percona MongoDB Overview** | WiredTiger cache, oplog window, index usage |
| **ArgoCD** | Application sync status, health status, reconciliation time |
| **PMM Overview** | Database fleet health, query analytics, slow query log |

### Alerting Rules

**Pre-configured VMRule alerts:**

| Alert | Condition | Severity |
|-------|-----------|----------|
| `KubePodCrashLooping` | Pod restarts > 3 in 15min | warning |
| `KubeNodeNotReady` | Node not ready > 5min | critical |
| `PersistentVolumeFillingUp` | PV usage > 85% | warning |
| `HighMemoryUsage` | Node memory > 90% | warning |
| `PostgreSQLDown` | PG instance down | critical |
| `PostgreSQLReplicationLag` | Lag > 10s | warning |
| `PostgreSQLHighConnections` | Connections > 80% max | warning |
| `PostgreSQLDeadlocks` | Deadlocks detected | warning |
| `MongoDBDown` | Mongo instance down | critical |
| `MongoDBReplicationLag` | Replication lag > 10s | warning |
| `MongoDBHighOpenCursors` | Open cursors > 10000 | warning |
| `pgBackRestStale` | Last backup > 25h ago | critical |
| `CertExpiryWarning` | Cert expires < 30d | warning |
| `CertExpiryCritical` | Cert expires < 7d | critical |
| `EdgeProxyDown` | Edge server down > 2min | critical |
| `EdgeProxyHighLatency` | P99 latency > 2s | warning |
| `EdgeCacheHitRateLow` | Hit rate < 50% | warning |

---

## Databases

### PostgreSQL

**Percona Distribution for PostgreSQL 18:**

- **HA**: 1-3 replicas (synchronous replication on production)
- **PgBouncer**: transaction pooling, 100 connections default
- **pgBackRest**: incremental backups to MinIO S3
  - Full: weekly (Sunday 2 AM)
  - Incremental: daily
  - Retention: 30 days
  - PITR: point-in-time recovery to any second
- **PMM integration**: query analytics, slow query log
- **Extensions**: pg_stat_statements, pg_repack, pgaudit

**Configuration:**

```yaml
# group_vars/all.yml or tier profile
postgresql:
  replicas: 3  # production
  storage: 50Gi
  resources:
    requests: {cpu: 500m, memory: 1Gi}
    limits: {cpu: 2, memory: 4Gi}
pgbouncer:
  replicas: 2
  pool_size: 100
backup:
  schedule: "0 2 * * *"  # daily 2 AM
  retention: 30  # days
```

**Access:**

```bash
# Via kubectl port-forward
kubectl port-forward -n databases svc/pg-main-pgbouncer 5432:5432
psql -h localhost -U postgres -d postgres

# From pod
kubectl exec -it -n databases pg-main-0 -- psql -U postgres
```

### MongoDB

**Percona Server for MongoDB 8.0:**

- **HA**: 1 or 3-member replica set
- **PBM**: backups to MinIO S3
  - Schedule: daily 2 AM
  - Retention: 30 days
  - Supports PITR
- **PMM integration**: profiler, replica set lag
- **Storage**: WiredTiger

**Configuration:**

```yaml
mongodb:
  replicas: 3  # production
  storage: 50Gi
  resources:
    requests: {cpu: 500m, memory: 1Gi}
    limits: {cpu: 2, memory: 2Gi}
```

**Access:**

```bash
kubectl port-forward -n databases svc/mongo-main 27017:27017
mongosh mongodb://localhost:27017
```

---

## Backup & Restore

### Backup Schedule (Medium/Production tiers)

| Component | Method | Schedule | Retention | Location |
|-----------|--------|----------|-----------|----------|
| PostgreSQL | pgBackRest | Daily 2 AM | 30 days | MinIO S3 |
| MongoDB | PBM | Daily 2 AM | 30 days | MinIO S3 |
| GitLab | gitlab-backup | Daily 2 AM | 30 days | MinIO S3 |
| Vault | Raft snapshot | Manual | — | Local/S3 |
| MinIO | Erasure coding | Continuous | — | Distributed |

### Restore Procedures

**PostgreSQL:**

```bash
# List backups
kubectl exec -n databases pg-main-0 -- pgbackrest info

# Restore to latest
kubectl exec -n databases pg-main-0 -- pgbackrest restore

# PITR (point-in-time recovery)
kubectl exec -n databases pg-main-0 -- pgbackrest restore \
  --type=time --target="2024-04-06 14:30:00"
```

**MongoDB:**

```bash
# List backups
kubectl exec -n databases mongo-main-0 -- pbm list

# Restore latest
kubectl exec -n databases mongo-main-0 -- pbm restore <backup-name>
```

**GitLab:**

```bash
# Via GitLab toolbox pod
kubectl exec -n gitlab <toolbox-pod> -- backup-utility --restore
```

---

## DNS Configuration

DNS records are automatically created via Hetzner DNS API:

| Record Type | Name | Points To | Purpose |
|-------------|------|-----------|--------|
| A | `*.<domain>` | LB IP or bastion | Wildcard for all HTTPRoutes |
| A | `<domain>` | LB IP or bastion | Root domain |
| A | `vpn.<domain>` | Bastion public IP | Headscale VPN server |
| A | `origin.<domain>` | LB IP or bastion | Edge CDN origin |
| A | `cdn.<domain>` | Gcore GeoDNS | Edge CDN endpoint |
| TXT | `_acme-challenge` | cert-manager | Let's Encrypt DNS01 validation |

**Important**: Existing DNS records are preserved. Only platform-related records are managed.

---

## Troubleshooting

### Common Issues

#### 1. PVC stuck in Pending

**Symptom:**
```
kubectl get pvc -A
NAME     STATUS    VOLUME   CAPACITY
vault    Pending
```

**Cause**: Hetzner CSI volume provisioning is slow through NAT gateway.

**Solution**: Wait 2-3 minutes. CSI driver retries automatically. Check:
```bash
kubectl get pods -n kube-system | grep hcloud-csi
kubectl logs -n kube-system <csi-pod>
```

#### 2. Vault pods not ready

**Symptom**: Vault pods stay in `0/1 Running`

**Cause**: Waiting for PVC provisioning or Raft leader election.

**Solution**:
```bash
# Check PVCs
kubectl get pvc -n vault

# Check Vault logs
kubectl logs -n vault vault-0

# Check Vault status
kubectl exec -n vault vault-0 -- vault status
```

#### 3. GitLab pods crash looping

**Symptom**: `gitlab-webservice` OOMKilled

**Cause**: Insufficient memory on minimal/small tier.

**Solution**: GitLab requires 4GB+ RAM. Use medium tier or increase worker size.

#### 4. SSH tunnel drops

**Symptom**: Ansible hangs during K8s tasks.

**Cause**: `run_tier.sh` SSH tunnel to bastion dropped.

**Solution**: Script auto-restarts tunnel. For manual runs:
```bash
ssh -L 6443:10.0.1.11:6443 root@<bastion-ip> -N -f
```

#### 5. Helm timeout

**Symptom**: `Helm install failed: timeout`

**Cause**: Large images (GitLab ~2GB) pull slowly through NAT.

**Solution**: All Helm installs use `wait: false`. Pod readiness checked separately:
```bash
kubectl wait --for=condition=ready pod -l app=gitlab-webservice -n gitlab --timeout=20m
```

### Useful Commands

**Cluster health:**
```bash
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running | grep -v Completed
kubectl top nodes
kubectl top pods -A
```

**Namespace status:**
```bash
kubectl get all -n vault
kubectl get all -n databases
kubectl get all -n gitlab
kubectl get all -n monitoring
```

**Vault:**
```bash
kubectl exec -n vault vault-0 -- vault status
kubectl exec -n vault vault-0 -- vault operator raft list-peers
```

**Databases:**
```bash
kubectl get perconapgclusters -n databases
kubectl get psmdb -n databases
kubectl exec -n databases pg-main-0 -- psql -U postgres -c 'SELECT version();'
```

**Logs:**
```bash
kubectl logs -n <namespace> <pod> --tail=100 -f
kubectl logs -n <namespace> <pod> --previous  # crashed pod logs
```

**NetworkPolicy debugging:**
```bash
# Check Hubble (Cilium observability)
kubectl exec -n kube-system <cilium-pod> -- hubble observe --verdict DROPPED

# Check specific pod connectivity
kubectl exec -n production <pod> -- curl -v http://pg-main.databases.svc.cluster.local:5432
```

---

## Project Structure

```
ansible-k8s-full-setup/
├── playbooks/
│   ├── deploy_platform.yml          # Main orchestration playbook
│   ├── continue_post_kubespray.yml  # Resume after K8s install
│   ├── edge-cdn.yml                 # Edge CDN deployment
│   └── kubespray/                   # Kubespray submodule (v2.30.0)
├── roles/
│   ├── generate-secrets/            # Secret generation
│   ├── hetzner-infra/               # Cloud infrastructure (VMs, LB, FW, DNS)
│   ├── network-security/            # Bastion hardening, VPN, NAT, auditd
│   ├── k8s-cluster-management/      # CNI, Gateway API, cert-manager, CSI
│   ├── k8s-secrets/                 # Vault + External Secrets Operator
│   ├── minio-storage/               # S3 object storage
│   ├── k8s-observability/           # VictoriaMetrics, Grafana, Loki, Promtail
│   ├── k8s-databases/               # PostgreSQL + MongoDB operators
│   ├── elasticsearch/               # ELK stack (HIPAA audit logs)
│   ├── gitlab-selfhosted/           # GitLab CE + Runner
│   ├── k8s-gitops/                  # ArgoCD + ApplicationSet
│   ├── k8s-autoscaling/             # KEDA autoscaler
│   ├── temporal/                    # Workflow engine
│   ├── dragonfly/                   # Redis-compatible cache
│   ├── postal/                      # SMTP email server
│   ├── opwerf-deployment/           # Sample app (platform UI)
│   ├── brocoders-boilerplate-setup/ # Sample NestJS+React app
│   ├── hipaa-hardening/             # HIPAA compliance (mTLS, audit, PII redaction)
│   └── edge-cdn/                    # Global edge proxy + Gcore GeoDNS
├── defaults/
│   └── main.yml                     # Global default variables
├── platform-orchestrator/
│   ├── platform.sh                  # CLI orchestrator
│   └── profiles/
│       ├── minimal.yaml             # Tier configs
│       ├── small.yaml
│       ├── medium.yaml
│       └── production.yaml
├── inventory/
│   └── hosts.yml                    # Generated during deployment
├── docs/
│   ├── DEPLOYMENT.md                # Detailed deployment guide
│   ├── HIPAA_COMPLIANCE.md          # HIPAA feature documentation
│   ├── SECURITY_OVERVIEW.md         # Complete security audit report
│   └── LOGGING_SECURITY_AUDIT.md    # Audit logging details
├── run_tier.sh                      # Quick tier deployment script
├── teardown.sh                      # Infrastructure cleanup
└── README.md                        # This file
```

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Test with at least the `minimal` tier
4. Validate YAML: `ansible-playbook --syntax-check playbooks/deploy_platform.yml`
5. Run security audit: See `SECURITY_OVERVIEW.md`
6. Submit a pull request

---

## License

MIT License - see [LICENSE](LICENSE) file

---

## Credits

- **Kubespray**: https://github.com/kubernetes-sigs/kubespray
- **Cilium**: https://cilium.io/
- **HashiCorp Vault**: https://www.vaultproject.io/
- **Percona Operators**: https://www.percona.com/software/percona-kubernetes-operators
- **VictoriaMetrics**: https://victoriametrics.com/
- **Gcore**: https://gcore.com/
- **Hetzner Cloud**: https://www.hetzner.com/cloud

---

## Support

- **Issues**: https://github.com/nmime/ansible-k8s-full-setup/issues
- **Discussions**: https://github.com/nmime/ansible-k8s-full-setup/discussions
- **Documentation**: See `docs/` directory

---

**Built with ❤️ for production Kubernetes on Hetzner Cloud**
