# Production Kubernetes Platform with Global Edge CDN

> Zero-to-production Kubernetes on Hetzner Cloud in 3-5 hours. Fully automated, security-hardened, with global GeoDNS edge network.

[![Security](https://img.shields.io/badge/security-100%25%20hardened-brightgreen)](#security-hardening)
[![Kubernetes](https://img.shields.io/badge/kubernetes-v1.35.6-326ce5?logo=kubernetes&logoColor=white)](#platform-stack)
[![HIPAA](https://img.shields.io/badge/HIPAA-ready-blue)](#compliance)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](#license)

---

## What You Get

**Core Infrastructure:**
- ✅ HA Kubernetes (3 control planes, 2+ workers)
- ✅ Private network only (bastion + VPN access)
- ✅ Production-grade security (NetworkPolicies, PSA, TLS everywhere)
- ✅ Global edge CDN with GeoDNS (EU/US/APAC)
- ✅ Automated backups (S3)
- ✅ Full monitoring stack (Grafana + 12 dashboards)

**Application:**
- nest-react-boilerplate (deployed via ArgoCD from `nest-react-boilerplate/deploy/k8s/`)

**Required Components** (always installed):
- Cilium CNI (eBPF networking + policies)
- cert-manager (automated TLS)
- Gateway API (modern ingress)
- HashiCorp Vault (secrets management)
- SeaweedFS (S3-compatible object storage)
- VictoriaMetrics + Grafana (monitoring)
- Loki + Promtail (logging)
- Headscale VPN (WireGuard)
- GitLab (Git + CI/CD + Registry)
- ArgoCD (GitOps continuous delivery)
- Elasticsearch (audit logs + compliance)

Object storage note: RustFS was evaluated as an Apache-2.0 future/alternative backend, but SeaweedFS is selected for this release because RustFS Helm/app remain beta while SeaweedFS has a mature chart and live validation.

**Optional Components** (install as needed):
- PostgreSQL 18 HA (Percona Operator)
- MongoDB 8.0 HA (Percona Operator)
- Dragonfly (Redis v6-compatible, 25x faster)
- Temporal (workflow orchestration)

---

## Quick Start

### Prerequisites

### Application: nest-react-boilerplate

The platform deploys the **nest-react-boilerplate** application via ArgoCD GitOps.
The application manifests are located in the `nest-react-boilerplate/deploy/k8s/` directory.

**Requirements:**
- **Node.js 24** — required for the build environment
- **Dragonfly** — provides Redis v6-compatible caching (deployed by default)

```bash
# 1. Create Hetzner API token
https://console.hetzner.cloud/ → Security → API Tokens → Generate

# 2. (Optional) Create Gcore API key for edge CDN
https://gcore.com/ → Account → API Tokens → Create

# 3. Install dependencies
sudo apt update && sudo apt install -y ansible git python3-pip
pip3 install jinja2 netaddr
```

### Deploy

```bash
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup

# Configure your domain and API keys
cp group_vars/all.yml.example group_vars/all.yml
vim group_vars/all.yml  # Set domain, hcloud_token, etc.

# Deploy full platform (3-5 hours first run)
ansible-playbook -i inventory/hosts.yml site.yml
```

**What gets deployed:**
1. Hetzner infrastructure (bastion, control planes, workers, LB, firewall)
2. Kubernetes cluster (HA, 3 CP + 2 workers)
3. Core platform (Cilium, Vault, Gateway API, cert-manager)
4. Monitoring (VictoriaMetrics, Grafana, Loki)
5. Storage (SeaweedFS S3)
6. VPN (Headscale)
7. Application: nest-react-boilerplate (via ArgoCD GitOps)
8. Edge CDN (optional, if `GCORE_API_KEY` set)

---

## Architecture

### Infrastructure Overview

```
                          INTERNET
                             │
         ┌───────────────────┼──────────────────┐
         │                   │                  │
    Gcore GeoDNS      Hetzner LB          VPN (WireGuard)
  app.domain.com      api.domain.com    vpn.domain.com
         │                   │                  │
   ┌─────┴────┐         ┌────┴────┐      ┌─────┴─────┐
 EU Edge  US Edge    Public Gateway   Headscale VPN
  (fsn1)  (ash)         :80/:443         (bastion)
   │         │               │                │
   └─────────┴───────────────┼────────────────┘
                             │
              ┌──────────────┴──────────────┐
              │   HETZNER PRIVATE NETWORK   │
              │       10.0.0.0/16          │
              │                             │
    ┌─────────┼─────────────────┬──────────┼────┐
 Bastion  Control Planes (HA)  Workers (HA)
    │     10.0.1.0/24         10.0.2.0/24
    │      ├─ CP1              ├─ Worker1
    │      ├─ CP2              ├─ Worker2
    │      └─ CP3              └─ Worker3
    │
    ├─ NAT Gateway
    ├─ VPN Server (Headscale)
    ├─ Fail2ban + UFW
    └─ Audit logging
```

### Edge CDN Flow

```
User (Asia)  User (EU)   User (US)
    │            │          │
    ▼            ▼          ▼
  GeoDNS routing (continent-based)
    │            │          │
    ▼            ▼          ▼
APAC Edge   EU Edge    US Edge
(Singapore) (Germany)  (US East)
    │            │          │
    └────────────┴──────────┘
              │
       Cache HIT? → Serve from edge (30d static, 1h HTML)
       Cache MISS? → Fetch from origin
              │
              ▼
    origin.domain.com (K8s LB)
```

---

## Platform Stack

### Core (Always Installed)

| Component | Version | Purpose |
|-----------|---------|--------|
| **Kubernetes** | v1.35.6 | Orchestration (Kubespray 2.31) |
| **Cilium** | v1.19.5 | eBPF CNI + NetworkPolicies |
| **Gateway API** | v1.6 | Modern ingress (L7) |
| **cert-manager** | v1.21.0 | Automated TLS (Let's Encrypt) |
| **Vault** | v1.21 | Secrets management (HA Raft) |
| **SeaweedFS** | v5.4 | S3-compatible storage |
| **VictoriaMetrics** | v1.133 | Metrics (faster than Prometheus) |
| **Grafana** | v12.3 | Monitoring dashboards |
| **Loki + Promtail** | v3.6 | Log aggregation |
| **Headscale** | v0.28 | WireGuard VPN |
| **MetalLB** | v0.16 | Bare-metal LB |
| **GitLab** | v18.11.3 | Git + CI/CD + Registry |
| **ArgoCD** | v3.4.2 | GitOps continuous delivery |
| **Elasticsearch** | v9.4.3 | Audit logs + compliance |

### Optional (Install as Needed)

| Component | Version | Install Flag | Purpose |
|-----------|---------|-------------|--------|
| **PostgreSQL 18** | Percona 2.8 | `install_postgresql: true` | HA database with pgBackRest |
| **MongoDB 8.0** | Percona 1.22 | `install_mongodb: true` | HA NoSQL with PBM backups |
| **Dragonfly** | v1.39.0 | `install_dragonfly: true` | Redis v6-compatible (25x faster) |
| **Temporal** | v1.31.0 | `install_temporal: true` | Workflow orchestration |
| **PMM** | v3 | Auto-enabled with DBs | Percona database monitoring |

**Note**: PMM is auto-enabled when PostgreSQL, MongoDB, or Dragonfly is installed.
**Enable in `group_vars/all.yml`:**
```yaml
install_postgresql: true
install_mongodb: false
install_dragonfly: true
install_temporal: false  # Heavy, only if needed
```

---

## Security Hardening

**Network Security:**
- ✅ All nodes private (no public IPs except bastion + LB)
- ✅ Bastion: UFW firewall (SSH, HTTPS, VPN only)
- ✅ Nodes: Private network only (10.0.0.0/16)
- ✅ Fail2ban on bastion + edge proxies
- ✅ Hetzner Cloud Firewalls (fw-bastion, fw-nodes)

**Kubernetes Security:**
- ✅ 47 NetworkPolicies (default-deny everywhere)
- ✅ 17 namespaces with Pod Security Admission (PSA)
- ✅ No default ServiceAccount token mounting
- ✅ TLS everywhere (cert-manager + internal CA)
- ✅ Secrets encrypted at rest (K8s native AES-CBC)
- ✅ Vault for external secrets (HA Raft, auto-unseal)

**Edge CDN WAF:**
- ✅ Rate limiting: 50 req/s general, 20 req/s API (per IP)
- ✅ Bad bot blocking (ahrefs, semrush, scanners, empty UA)
- ✅ Block dangerous methods (TRACE, TRACK, DEBUG)
- ✅ Anti-slowloris: 10s client timeouts
- ✅ Attack path blocking (.env, .git, wp-admin, phpmyadmin)
- ✅ Fail2ban nginx-req-limit (bans rate-limited IPs for 24h)

**Compliance:**
- ✅ HIPAA-ready (audit logs, encryption, access controls)
- ✅ Vault audit logs → Elasticsearch
- ✅ K8s audit logs → Elasticsearch
- ✅ Host audit logs (auditd) → Loki

---

## Edge CDN Configuration

### DNS Provider Options

Choose ONE DNS provider mode:

**Option 1: All DNS in Gcore (Recommended)**
```yaml
edge_dns_provider: "gcore"
edge_cdn_domains:
  - "app.{{ domain }}"
  - "dash.{{ domain }}"
edge_direct_domains:
  - "vpn.{{ domain }}"
  - "api.{{ domain }}"
```
- **Setup**: Change NS at registrar to Gcore nameservers
- **Result**: `app.domain.com` → GeoDNS → nearest edge → cached
- **Best for**: Production (simplest, fastest)

**Option 2: Keep Hetzner DNS + NS delegation**
```yaml
edge_dns_provider: "hetzner"
```
- **Setup**: At registrar, delegate CDN subdomains to Gcore via NS records
- **Result**: CDN domains use GeoDNS, rest stay in Hetzner
- **Best for**: When you can't move NS to Gcore

**Option 3: Keep Hetzner DNS + CNAME**
```yaml
edge_dns_provider: "hetzner_cname"
```
- **Setup**: No NS delegation, uses CNAME to Gcore
- **Result**: Slower (+1 DNS lookup), but no registrar changes
- **Best for**: Testing or when NS delegation is blocked

**Deploy edge CDN:**
```bash
ansible-playbook -i inventory/hosts.yml playbooks/edge-cdn.yml \
  -e "edge_dns_provider=gcore" \
  -e "domain=example.com" \
  -e "origin_server_ip=<K8s_LB_IP>"
```

See [docs/CDN_ROUTING_MODES.md](docs/CDN_ROUTING_MODES.md) for full details.

---

## Monitoring

**Access Grafana:**
```bash
# Via VPN
https://grafana.domain.com

# Credentials
kubectl get secret -n monitoring grafana-admin-credentials -o jsonpath='{.data.password}' | base64 -d
```

**Pre-built Dashboards:**
- Kubernetes Cluster Overview
- Node Resources (CPU, RAM, disk)
- Cilium Network Policies
- Gateway API Traffic
- PostgreSQL Performance (if enabled)
- MongoDB Performance (if enabled)
- SeaweedFS S3 Statistics
- Vault Metrics
- Edge CDN Cache Hit Rate
- Certificate Expiry
- ArgoCD Applications (if enabled)
- GitLab CI/CD (if enabled)

**Alerts:**
- Certificate expiry (< 30 days)
- Pod CrashLoopBackOff
- Node resource pressure
- Persistent volume 80% full
- Database replication lag
- Edge proxy down
- Vault sealed

---

## Deployment Tiers

Costs below use the current Hetzner server prices from the CX selector:
`cx23` = €5.49/mo (€0.009/h), `cx33` = €8.49/mo (€0.014/h), and `cx43` = €15.99/mo.
They are server-compute subtotals only unless a load balancer is called out separately.
Storage volumes, backups/snapshots, traffic overage, IPv4/floating IPs, edge/CDN
services, and tax/VAT can change the final bill.

### Current observed K8s footprint

```text
4 x cx23 = €21.96/mo
4 x cx33 = €33.96/mo
Server compute subtotal = €55.92/mo (€0.092/h, €671.04/year)
```

### Medium-Optimized / Production Tool Set

Use `platform-orchestrator/profiles/medium-optimized.yaml` when you want the
medium/production platform tools without paying for `cx43` nodes. It keeps
`tier: medium` so the platform still deploys the medium tool set, but overrides
the Hetzner shape to:

```text
3 x cx23 control plane
4 x cx33 workers
1 x cx23 bastion
1 x lb11
Baseline = €55.92/mo server compute, ~€61.92/mo with lb11
```

This profile enables GitLab, ArgoCD, Vault/ESO, distributed SeaweedFS, databases,
observability, Elasticsearch/APM, Dragonfly, Temporal, Postal, GlitchTip, Blackbox,
KEDA, and backups. For stricter production isolation, set
`hetzner_tier_specs.medium.cp_schedulable=false` and
`hetzner_tier_specs.medium.worker_count=5` for ~€70.41/mo with lb11.

```bash
cd platform-orchestrator
./platform.sh init medium-optimized
```

### Tier 1: Minimal / Development (~€16.47/mo server compute)
```yaml
control_plane_count: 1
worker_count: 1
server_type: "cx23"  # 2 vCPU, 4GB RAM
control_server_type: "cx23"
bastion_server_type: "cx23"  # 2 vCPU, 4GB RAM
edge_enabled: false
```
**Use case**: Testing, dev environments

### Tier 2: Small / Staging (~€21.96/mo server compute, ~€27.96/mo with lb11)
```yaml
control_plane_count: 1
worker_count: 2
server_type: "cx23"  # 2 vCPU, 4GB RAM
control_server_type: "cx23"
bastion_server_type: "cx23"  # 2 vCPU, 4GB RAM
edge_enabled: false
load_balancer: "lb11"
```
**Use case**: Small teams, staging

### Tier 3: Medium / HA Staging (~€85.44/mo server compute, ~€91.44/mo with lb11)
```yaml
control_plane_count: 3
worker_count: 2
server_type: "cx43"  # 16Gi-class RAM
control_server_type: "cx43"
edge_enabled: true
gitlab_edition: "ee"
edge_regions: ["eu"]  # Single edge
```
**Use case**: Pre-production, staging

### Tier 4: Production (~€101.43/mo server compute, ~€107.43/mo with lb11)
```yaml
control_plane_count: 3
worker_count: 3
server_type: "cx43"  # 16Gi-class RAM
control_server_type: "cx43"
edge_enabled: true
gitlab_edition: "ee"
edge_regions: ["eu", "us", "apac"]  # Global CDN
```
**Use case**: Production workloads, HA required

### Tier 5: Enterprise (€200+/mo)
```yaml
control_plane_count: 5
worker_count: 5
server_type: "cx52"  # 16 vCPU, 32GB
control_server_type: "cx43"
edge_enabled: true
gitlab_edition: "ee"
```
**Use case**: HIPAA compliance, high traffic

---

## Backup & Restore

**Automated backups** (every 6 hours to SeaweedFS S3):
- PostgreSQL: pgBackRest (PITR)
- MongoDB: PBM (point-in-time)
- Vault: Raft snapshots
- etcd: Automated snapshots

**Restore PostgreSQL:**
```bash
kubectl exec -n databases deploy/pg-cluster-1 -- \
  pgbackrest --stanza=db restore --type=time --target="2024-01-15 10:00:00"
```

**Restore MongoDB:**
```bash
kubectl exec -n databases sts/mongo-cluster-rs0-0 -- \
  pbm restore --time="2024-01-15T10:00:00Z"
```

---

## Configuration Examples

### Minimal Production Setup

**`group_vars/all.yml`:**
```yaml
# Domain
domain: "example.com"
admin_email: "admin@example.com"

# Hetzner
hcloud_token: "{{ lookup('env', 'HCLOUD_TOKEN') }}"
project_name: "prod-k8s"

# Cluster size
control_plane_count: 3
worker_count: 3
server_type: "cx43"

# Core platform (always on)
install_cilium: true
install_cert_manager: true
install_vault: true
install_object_storage: true
install_monitoring: true
install_vpn: true

# Optional components
install_postgresql: true
install_mongodb: false
install_dragonfly: true
install_temporal: false

# Edge CDN
edge_dns_provider: "gcore"
gcore_api_key: "{{ lookup('env', 'GCORE_API_KEY') }}"
edge_cdn_domains:
  - "app.{{ domain }}"
  - "dash.{{ domain }}"
edge_direct_domains:
  - "vpn.{{ domain }}"
  - "api.{{ domain }}"
```

**Deploy:**
```bash
# See .env.example for all required environment variables
# cp .env.example .env && source .env
export HCLOUD_TOKEN="your-hetzner-token"
export GCORE_API_KEY="your-gcore-token"
export GITHUB_TOKEN="your-github-token"  # For GitLab mirrors

ansible-playbook -i inventory/hosts.yml site.yml
```

---

## Troubleshooting

### Check cluster health
```bash
kubectl get nodes
kubectl get pods -A | grep -v Running
kubectl top nodes
```

### Check edge CDN
```bash
# Test GeoDNS routing
dig +short app.example.com

# Check cache status
curl -I https://app.example.com/
# Look for: X-Cache-Status: HIT
#           X-Edge-Region: EU
```

### Check certificates
```bash
kubectl get certificate -A
kubectl describe certificate -n production app-tls
```

### View logs
```bash
# Application logs
kubectl logs -n production deploy/myapp -f

# Ingress logs
kubectl logs -n gateway-system deploy/cilium-gateway -f

# All logs via Loki (in Grafana)
https://grafana.domain.com/explore
```

### Vault unsealed?
```bash
kubectl exec -n vault vault-0 -- vault status
```

### GitLab not starting?
```bash
# Check resources (GitLab needs 8GB+ RAM per pod)
kubectl describe pod -n gitlab gitlab-webservice-xxx
```

---

## Documentation

- [Security Overview](SECURITY_OVERVIEW.md) — Full security architecture
- [DNS & Traffic Flow](docs/DNS_AND_TRAFFIC_FLOW.md) — Routing explained
- [CDN Routing Modes](docs/CDN_ROUTING_MODES.md) — GeoDNS setup
- [Configure CDN for Frontend](docs/CONFIGURE_CDN_FOR_FRONTEND.md) — App integration
- [Edge CDN Usage Guide](docs/EDGE_CDN_USAGE_GUIDE.md) — Operations

---

## License

MIT License. See [LICENSE](LICENSE) for details.

---

## Support

Issues: https://github.com/nmime/ansible-k8s-full-setup/issues

Pull requests welcome!
