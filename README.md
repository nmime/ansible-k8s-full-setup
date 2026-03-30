# Ansible Kubernetes Full-Stack Platform

> Production-grade Kubernetes platform on Hetzner Cloud — from bare metal to running applications in a single command. Fully automated, security-hardened, and cost-optimized.

[![Tested](https://img.shields.io/badge/tested-4%2F4%20tiers%20passed-brightgreen)](#deployment-tiers)
[![Kubernetes](https://img.shields.io/badge/kubernetes-v1.34.3-326ce5?logo=kubernetes&logoColor=white)](#tech-stack)
[![License](https://img.shields.io/badge/license-MIT-blue)](#license)

---

## What This Does

One Ansible playbook provisions **everything** — cloud infrastructure, Kubernetes cluster, service mesh, databases, GitLab, monitoring, GitOps, autoscaling, and workflow engine. Choose a tier, point it at Hetzner Cloud, and get a fully working platform.

**Key highlights:**
- Zero-to-production in **~3–5 hours** (fully automated, no manual steps)
- **4 deployment tiers** from €18/mo dev to €100/mo production HA
- **Private-only architecture** — all nodes behind bastion + VPN, no public IPs
- **12 integrated platform services** with security hardened by default
- **Idempotent** — safe to re-run at any point

---

## Architecture

```
                    ┌─────────────────────────────────────────────────────────────┐
                    │                     HETZNER CLOUD                          │
                    │                                                             │
    Internet        │   ┌──────────────┐     Private Network 10.0.0.0/16         │
        │           │   │   BASTION     │                                         │
        │           │   │  (public IP)  │    ┌─────────────────────────────┐      │
  ┌─────┴─────┐     │   │              │    │  10.0.1.0/24 Control Plane  │      │
  │   Users   ├─────┼──►│ • Headscale  │    │  ┌─────┐ ┌─────┐ ┌─────┐  │      │
  │           │     │   │ • NAT Gateway├───►│  │ CP1 │ │ CP2 │ │ CP3 │  │      │
  └─────┬─────┘     │   │ • fail2ban   │    │  └─────┘ └─────┘ └─────┘  │      │
        │           │   │ • UFW        │    └─────────────────────────────┘      │
        │           │   └──────┬───────┘                                         │
  ┌─────┴─────┐     │          │             ┌─────────────────────────────┐      │
  │ Load      │     │          │             │  10.0.2.0/24 Workers        │      │
  │ Balancer  ├─────┼──────────┼────────────►│  ┌─────┐ ┌─────┐ ┌─────┐  │      │
  │ (lb11)    │     │          │             │  │ W1  │ │ W2  │ │ W3  │  │      │
  └───────────┘     │          │             │  └─────┘ └─────┘ └─────┘  │      │
                    │          │             └─────────────────────────────┘      │
                    │     VPN: Headscale                                          │
                    │     10.0.0.0/16 + 100.64.0.0/10                            │
                    └─────────────────────────────────────────────────────────────┘

  Firewalls:
    fw-bastion: SSH(22), HTTPS(443), STUN(3478), WireGuard(41641) from 0.0.0.0/0
    fw-nodes:   All traffic restricted to private+VPN ranges only
```

### Traffic Flow

**Public traffic (Boilerplate app, MinIO S3 API):**

```
Internet → Hetzner LB (:80/:443)
         → TCP passthrough to NodePort :30080/:30443 on worker nodes (private network)
         → Cilium Gateway (envoy proxy, TLS termination with wildcard cert)
         → HTTPRoute matching (hostname/path based)
         → Backend service pods
```

**Admin traffic (GitLab, Registry, KAS, Grafana, ArgoCD, Vault, PMM, MinIO Console):**

```
VPN client (100.64.0.0/10) → admin-gateway NodePort :31443
                           → Cilium Gateway (TLS termination)
                           → HTTPRoute → admin service pods
```

- **Hetzner LB** (lb11) listens on ports 80/443, forwards via TCP passthrough to fixed NodePorts 30080/30443 on all worker nodes through the private network
- **Cilium Gateway API** (`main-gateway`) terminates TLS using the wildcard Let's Encrypt certificate and routes requests to backend services via HTTPRoutes
- **GitLab Registry** is behind VPN (`admin-gateway`) — kubelet pulls images via containerd registry mirror configured to use the in-cluster service (`gitlab-registry.gitlab.svc.cluster.local:5000`) instead of the public domain
- **Admin gateway** uses a separate `admin-gateway` on NodePort 31443, protected by CiliumNetworkPolicy restricting access to VPN (`100.64.0.0/10`) and private network (`10.0.0.0/16`) only
- **MetalLB** (`v0.15.3`) assigns private VIPs from `10.0.10.0/24` (L2 mode) for internal/VPN access — it does **not** handle internet traffic
- **Minimal tier exception:** no Hetzner LB is created (saves ~€6/mo) — the bastion proxies public traffic directly

---

## Tech Stack

### Core Platform

| Component | Version | Purpose |
|-----------|---------|--------|
| **Kubernetes** | `v1.34.3` | Container orchestration (via Kubespray `v2.30.0`) |
| **Cilium** | `v1.19.2` | CNI, eBPF networking, network policies, Hubble observability |
| **Gateway API** | `v1.5.1` | Ingress routing (replaces legacy Ingress) |
| **cert-manager** | `v1.20.0` | Automated TLS certificates (Let's Encrypt + DNS01) |
| **MetalLB** | `v0.15.3` | Bare-metal load balancer (L2 mode) |
| **Hetzner CCM** | `v1.30.1` | Cloud controller manager |
| **Hetzner CSI** | `v2.20.0` | Persistent volume provisioning |

### Platform Services

| Component | Version | Purpose |
|-----------|---------|--------|
| **HashiCorp Vault** | `v1.21.2` (chart `0.32.0`) | Secrets management, auto-unseal, HA Raft |
| **External Secrets Operator** | `v0.15.0` (chart `2.2.0`) | Kubernetes ↔ Vault secret sync |
| **MinIO** | chart `5.4.0` | S3-compatible object storage |
| **PostgreSQL 18** | Percona Operator `2.8.2` | HA PostgreSQL with PgBouncer + pgBackRest |
| **MongoDB 8.0** | Percona Operator `1.22.0` | Replicated MongoDB with PBM backups |
| **GitLab EE** | `v18.10.0` (chart `9.10.0`) | Source code, CI/CD, Container Registry, KAS |
| **GitLab Runner** | chart `0.87.0` | CI/CD job execution |
| **ArgoCD** | `v3.3.4` (chart `9.4.15`) | GitOps continuous delivery |
| **VictoriaMetrics** | `v1.133.0` (operator `0.59.3`) | Metrics collection & storage |
| **Grafana** | `v12.3.1` (chart `10.5.15`) | Dashboards & visualization (12 pre-built dashboards) |
| **Loki** | `v3.6.3` (chart `6.55.0`) | Log aggregation |
| **Promtail** | chart `6.17.1` | Log shipping |
| **PMM Server** | `v3` | Percona Monitoring & Management |
| **KEDA** | `v2.18.2` (chart `2.19.0`) | Event-driven autoscaling |
| **Temporal** | `v1.29.1` (chart `0.73.2`) | Workflow orchestration engine |
| **Headscale** | `v0.28.0` | Self-hosted WireGuard VPN (Tailscale-compatible) |

### Database Stack Details

| Component | Image | Purpose |
|-----------|-------|--------|
| PostgreSQL | `percona/percona-distribution-postgresql:18.3-1` | Primary database |
| PgBouncer | `percona/percona-pgbouncer:1.25.1-1` | Connection pooling |
| pgBackRest | `percona/percona-pgbackrest:2.58.0-1` | Backup & PITR to MinIO |
| MongoDB | `percona/percona-server-mongodb:8.0` | Document database |
| PBM | `percona/percona-backup-mongodb:2.13.0` | MongoDB backup to MinIO |
| PMM Client | `percona/pmm-client:2.44.0` | Database monitoring agent |

---

## Deployment Tiers

### Overview

| | **Minimal** | **Small** | **Medium** | **Production** |
|---|---|---|---|---|
| **Cost** | ~€18–20/mo | ~€28–35/mo | ~€48–55/mo | ~€74–100/mo |
| **Best for** | Dev / Learning | Startups / Staging | Small-medium teams | Production workloads |
| **Deploy time** | ~3 hours | ~3 hours | ~5 hours | ~5 hours |
| **Nodes** | 2 | 3 | 5 | 6+ |
| **Control plane** | 1× cx23 | 1× cx23 | 3× cx23 (HA) | 3× cx33 (HA) |
| **Workers** | 1× cx23 | 2× cx23 | 2× cx33 | 3× cx33 |
| **CP schedulable** | ✅ | ❌ | ❌ | ❌ |
| **Load balancer** | ❌ (bastion proxy) | ✅ lb11 | ✅ lb11 | ✅ lb11 |
| **Placement group** | ❌ | ❌ | ✅ spread | ✅ spread |

### Component Scaling per Tier

| Component | Minimal | Small | Medium | Production |
|-----------|---------|-------|--------|------------|
| **Vault** | 1 standalone | 1 standalone | 3 HA Raft | 3 HA Raft |
| **MinIO** | 1 standalone, 50Gi | 1 standalone, 100Gi | 4 distributed, 50Gi×4 | 4 distributed, 100Gi×4 |
| **PostgreSQL** | 1 replica, 10Gi | 1 replica, 20Gi | 2 replicas, 20Gi | 3 replicas, 50Gi |
| **PgBouncer** | 1 | 1 | 2 | 2 |
| **MongoDB** | 1 | 1 | 3 replicas | 3 replicas |
| **VictoriaMetrics** | VMSingle, 10Gi | VMSingle, 20Gi | VMCluster, 50Gi | VMCluster, 100Gi |
| **Loki** | SingleBinary | SingleBinary | SimpleScalable | SimpleScalable |
| **Metrics retention** | 7 days | 14 days | 30 days | 30 days |
| **Log retention** | 3 days | 7 days | 14 days | 14 days |
| **Grafana** | 1 | 1 | 1 | 2 |
| **GitLab webservice** | 1 | 1 | 2 | 2 |
| **GitLab sidekiq** | 1 | 1 | 2 | 2 |
| **Runner concurrency** | 2 | 5 | 10 | 20 |
| **ArgoCD** | standalone | standalone | HA (2) | HA (2) |
| **cert-manager** | 1 | 2 | 2 | 3 |
| **KEDA** | 1 | 1 | 2 | 2 |
| **Temporal frontend** | 1 | 1 | 1 | 2 |
| **ESO** | 1 | 1 | 2 | 2 |
| **Backup** | ❌ | ❌ | ✅ daily 2AM, 30d | ✅ daily 2AM, 30d |

---

## Prerequisites

| Tool | Version | Install |
|------|---------|--------|
| Ansible | ≥ 2.17 | `pip install ansible` |
| `hcloud` CLI | latest | [hetznercloud/cli](https://github.com/hetznercloud/cli) |
| `kubectl` | ≥ 1.30 | [kubernetes.io](https://kubernetes.io/docs/tasks/tools/) |
| `helm` | ≥ 3.14 | [helm.sh](https://helm.sh/docs/intro/install/) |
| `yq` | ≥ 4.0 | [mikefarah/yq](https://github.com/mikefarah/yq) |
| SSH key | Ed25519 | `ssh-keygen -t ed25519` |

**Ansible collections** (installed automatically):
```
community.general >= 9.0.0
kubernetes.core   >= 4.0.0
```

**Environment variable:**
```bash
export HCLOUD_TOKEN="your-hetzner-api-token"
```

---

## Quick Start

### Option 1: Platform Orchestrator (Recommended)

```bash
# Clone
git clone https://github.com/nmime/ansible-k8s-full-setup.git
cd ansible-k8s-full-setup

# Configure
cp platform-orchestrator/profiles/small.yaml platform-orchestrator/platform.yaml
# Edit platform.yaml — set domain and email

# Deploy
export HCLOUD_TOKEN="your-token"
./platform-orchestrator/platform.sh deploy
```

### Option 2: Direct Ansible

```bash
# Minimal tier
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=minimal \
  -e domain=example.com \
  -e email=admin@example.com \
  -e project_name=k8s

# Production tier
ansible-playbook playbooks/deploy_platform.yml \
  -e tier=production \
  -e domain=example.com \
  -e email=admin@example.com \
  -e project_name=k8s
```

### Option 3: Component-by-Component

```bash
# Deploy only infrastructure
ansible-playbook playbooks/deploy_platform.yml --tags infrastructure -e ...

# Deploy only databases
ansible-playbook playbooks/deploy_platform.yml --tags databases -e ...
```

**Available tags:** `infrastructure`, `network`, `security`, `cluster`, `kubernetes`, `secrets`, `vault`, `storage`, `minio`, `observability`, `monitoring`, `databases`, `postgresql`, `gitlab`, `cicd`, `gitops`, `argocd`, `autoscaling`, `keda`, `temporal`, `workflows`, `boilerplate`, `application`

---

## Platform Orchestrator Commands

```bash
./platform-orchestrator/platform.sh <command>
```

| Command | Description |
|---------|------------|
| `init` | Validate config, check prerequisites |
| `deploy` | Full platform deployment |
| `status` | Show current platform status |
| `health` | Health check all components |
| `credentials` | Display all service credentials |
| `heal` | Auto-fix common issues |
| `destroy` | Tear down all infrastructure |

---

## Deployment Flow

The playbook executes 12 roles sequentially:

```
 1. generate-secrets      Generate all platform credentials (idempotent)
 2. hetzner-infra          Provision VPC, subnets, firewalls, bastion, nodes, LB, DNS
 3. network-security       Bastion hardening, NAT gateway, Headscale VPN
 4. k8s-cluster-management Kubespray K8s install, Cilium, Gateway API, cert-manager, CCM/CSI
 5. k8s-secrets            Vault (standalone/HA), auto-init/unseal, ESO + ClusterSecretStore
 6. minio-storage          MinIO S3 (standalone/distributed), pre-created buckets
 7. k8s-observability      VictoriaMetrics, Loki, Promtail, Grafana (12 dashboards), PMM, alerting
 8. k8s-databases          PostgreSQL 18 HA + PgBouncer + pgBackRest, MongoDB 8.0 + PBM
 9. gitlab-selfhosted      GitLab EE Ultimate + Runner + Registry + KAS
10. k8s-gitops             ArgoCD (standalone/HA), multi-env ApplicationSet
11. k8s-autoscaling        KEDA event-driven autoscaler
12. temporal               Temporal workflow engine + Web UI
```

---

## Security

This platform is designed with defense-in-depth. Every layer is hardened by default.

### Network Isolation

- **Private-only nodes** — all K8s nodes have no public IPs (`--without-ipv4 --without-ipv6`)
- **Bastion-only entry** — single public-facing server with Headscale VPN
- **NAT gateway** — bastion provides outbound connectivity for nodes
- **Cloud firewalls** — Hetzner firewalls restrict bastion (SSH/VPN only) and nodes (private+VPN ranges only)
- **Default-deny NetworkPolicies** — every namespace gets deny-all ingress+egress, then explicit allow rules
- **CiliumNetworkPolicies** — fine-grained L7 policies: DNS, API server, intra-namespace, internet egress per namespace
- **Admin endpoints VPN-only** — Grafana, ArgoCD, Vault, PMM restricted to `10.0.0.0/16` + `100.64.0.0/10`

### Host Hardening

- **UFW** — deny incoming by default; allow only SSH(22), HTTPS(443), STUN(3478), WireGuard(41641)
- **fail2ban** — SSH brute-force protection (ban after 5 attempts, 1h ban, 10min window)
- **SSH hardening** — password auth disabled, root login key-only, max 5 auth tries

### Kubernetes Security

- **Pod Security Standards** — `enforce: baseline`, `warn: restricted`, `audit: restricted` on all namespaces
- **PodSecurity admission** — enabled cluster-wide via Kubespray
- **Kubernetes audit logging** — enabled
- **Certificate rotation** — kubelet certificates auto-rotate
- **IPVS proxy mode** — production-grade kube-proxy
- **RBAC** — ArgoCD default `role:readonly`, explicit admin grants

### Container Security

- `runAsNonRoot: true` on all platform workloads
- `capabilities.drop: [ALL]` across Vault, MinIO, ArgoCD, KEDA, Temporal, Headscale
- `allowPrivilegeEscalation: false` enforced
- `readOnlyRootFilesystem: true` where supported
- `seccompProfile: RuntimeDefault` on ArgoCD, KEDA, Temporal
- GitLab Runner: `privileged: false`, non-root execution

### Secrets Management

- **Vault** with Shamir's Secret Sharing (5 shares, 3 threshold)
- **KV v2** secrets engine with versioning
- **Kubernetes auth** method for pod-to-Vault authentication
- **External Secrets Operator** syncs Vault secrets → Kubernetes Secrets
- **Auto-generated credentials** — all passwords, tokens, API keys generated once and persisted to `.platform-secrets.yml` (gitignored)

### TLS Everywhere

- **cert-manager** with Let's Encrypt production ClusterIssuer
- **DNS01 challenges** via Hetzner DNS webhook for wildcard certs
- **Wildcard TLS** (`*.domain`) shared across all services
- **Per-service certificates** for GitLab, ArgoCD, Grafana, Vault, PMM, MinIO, Temporal
- **Caddy** TLS termination for Headscale VPN endpoint

---

## Service Access

After deployment, services are available at:

| Service | URL | Access |
|---------|-----|--------|
| **GitLab** | `https://gitlab.<domain>` | VPN only |
| **GitLab Registry** | `https://registry.<domain>` | VPN only |
| **GitLab KAS** | `https://kas.<domain>` | VPN only |
| **ArgoCD** | `https://argocd.<domain>` | VPN only |
| **Grafana** | `https://grafana.<domain>` | VPN only |
| **Vault** | `https://vault.<domain>` | VPN only |
| **PMM** | `https://pmm.<domain>` | VPN only |
| **MinIO Console** | `https://minio.<domain>` | VPN only |
| **MinIO S3 API** | `https://s3.<domain>` | Public (LB) |
| **Temporal Web** | `https://temporal.<domain>` | VPN only |
| **Headscale VPN** | `https://vpn.<domain>` | Public (bastion) |

### Getting Credentials

```bash
# Platform orchestrator
./platform-orchestrator/platform.sh credentials

# GitLab root password
kubectl get secret gitlab-gitlab-initial-root-password -n gitlab -o jsonpath='{.data.password}' | base64 -d

# ArgoCD admin password
kubectl get secret argocd-initial-admin-secret -n argocd -o jsonpath='{.data.password}' | base64 -d

# Grafana admin password
kubectl get secret grafana -n monitoring -o jsonpath='{.data.admin-password}' | base64 -d

# Vault root token
kubectl get secret vault-init-keys -n vault -o jsonpath='{.data.root_token}' | base64 -d
```

---

## VPN Access

All admin interfaces are behind Headscale VPN. To connect:

```bash
# 1. Create a pre-auth key on the bastion
ssh root@<bastion-ip>
docker exec headscale headscale preauthkeys create --reusable --expiration 24h

# 2. Install Tailscale client on your machine
# https://tailscale.com/download

# 3. Connect
tailscale up --login-server https://vpn.<domain> --authkey <preauthkey>
```

---

## Observability

### Pre-configured Grafana Dashboards

12 dashboards are automatically provisioned:

| Dashboard | Content |
|-----------|--------|
| Node Exporter Full | Host-level CPU, memory, disk, network |
| Kubernetes Cluster | Namespace workloads, pod status, resource usage |
| VictoriaMetrics | Ingestion rate, query performance, storage |
| Loki Dashboard | Log volume, error rates, ingestion |
| Cilium Overview | Network flows, drops, policy verdicts |
| PostgreSQL Overview | Connections, queries, replication, locks |
| MongoDB Overview | Operations, connections, replication |
| Percona PG Overview | Percona-specific PG metrics |
| Percona PG Replication | Replication lag, WAL, slots |
| Percona MongoDB Overview | Percona-specific Mongo metrics |
| ArgoCD | Sync status, app health, reconciliation |
| PMM Overview | Database fleet health |

### Alerting Rules

Pre-configured VMRule alerts:

- `KubePodCrashLooping` — pod restart > 3 in 15min
- `KubeNodeNotReady` — node not ready > 5min
- `PersistentVolumeFillingUp` — PV > 85% full
- `HighMemoryUsage` — node memory > 90%
- `PostgreSQLDown` / `PostgreSQLReplicationLag` / `PostgreSQLHighConnections` / `PostgreSQLDeadlocks`
- `MongoDBDown` / `MongoDBReplicationLag` / `MongoDBHighOpenCursors`
- `pgBackRestStale` — backup older than 25h

---

## Databases

### PostgreSQL

- **Percona Operator** manages HA PostgreSQL 18 clusters
- **PgBouncer** connection pooling (transaction mode, 100 default pool size)
- **pgBackRest** automated backups to MinIO S3 (configurable schedule + retention)
- **PMM integration** for monitoring
- **Custom `pg_hba.conf`** — MD5 auth for all connections
- Cross-namespace credential secrets for GitLab and Temporal

### MongoDB

- **Percona PSMDB Operator** manages replicated MongoDB 8.0 clusters
- **PBM** backups to MinIO S3
- **PMM integration** for monitoring
- WiredTiger storage engine

---

## Backup & Restore

Backups are enabled on **medium** and **production** tiers:

| What | How | Schedule | Retention |
|------|-----|----------|----------|
| PostgreSQL | pgBackRest → MinIO S3 | Daily 2:00 AM | 30 days |
| MongoDB | PBM → MinIO S3 | Daily 2:00 AM | 30 days |
| GitLab | Built-in backup → MinIO S3 | Configurable | 30 days |
| Vault | Raft snapshots | Manual / configurable | — |
| MinIO | Erasure coding (distributed mode) | Continuous | — |

---

## DNS Configuration

DNS is automatically configured via Hetzner DNS API:

| Record | Points to |
|--------|----------|
| `*.<domain>` | Load Balancer IP (or bastion for minimal) |
| `<domain>` | Load Balancer IP |
| `vpn.<domain>` | Bastion public IP |

The playbook only manages platform records — existing DNS records are preserved.

---

## Project Structure

```
.
├── playbooks/
│   ├── deploy_platform.yml          Main deployment playbook
│   ├── continue_post_kubespray.yml  Resume after Kubespray
│   └── kubespray/                   Kubespray submodule (v2.30.0)
├── roles/
│   ├── generate-secrets/            Credential generation
│   ├── hetzner-infra/               Cloud infrastructure provisioning
│   ├── network-security/            Bastion hardening, NAT, VPN
│   ├── k8s-cluster-management/      K8s install, CNI, cert-manager, CSI
│   ├── k8s-secrets/                 Vault + ESO
│   ├── minio-storage/               S3 object storage
│   ├── k8s-observability/           Metrics, logs, dashboards, alerting
│   ├── k8s-databases/               PostgreSQL + MongoDB operators
│   ├── gitlab-selfhosted/           GitLab EE + Runner
│   ├── k8s-gitops/                  ArgoCD + ApplicationSet
│   ├── k8s-autoscaling/             KEDA autoscaler
│   ├── temporal/                    Workflow engine
│   └── brocoders-boilerplate-setup/ Sample NestJS+React app (optional)
├── defaults/
│   └── main.yml                     Global default variables
├── platform-orchestrator/
│   ├── platform.sh                  CLI orchestrator
│   └── profiles/                    Tier configuration files
│       ├── minimal.yaml
│       ├── small.yaml
│       ├── medium.yaml
│       └── production.yaml
├── run_tier.sh                      Quick tier deployment script
└── teardown.sh                      Infrastructure cleanup
```

---

## Deployment Times

Measured on Hetzner Cloud (hel1 region), sequential deployment:

| Tier | Duration | Tasks | Breakdown |
|------|----------|-------|-----------|
| **Minimal** | ~3 hours | 329 | Infra 15m → K8s 20m → Services 2.5h |
| **Small** | ~3 hours | 337 | Infra 15m → K8s 20m → Services 2.5h |
| **Medium** | ~5 hours | 346 | Infra 20m → K8s 25m → Services 4h |
| **Production** | ~5 hours | 346 | Infra 20m → K8s 25m → Services 4h |

Most time is spent waiting for Helm deployments and image pulls on private-only nodes (all traffic routed through bastion NAT). Re-runs are significantly faster due to caching.

---

## Teardown

```bash
# Via orchestrator
./platform-orchestrator/platform.sh destroy

# Via script
./teardown.sh k8s-minimal

# Manual
hcloud server list | grep k8s-  # review before deleting
```

Teardown removes: servers, volumes, load balancer, firewalls, SSH keys, networks. DNS records are preserved.

---

## Troubleshooting

### Common Issues

| Issue | Solution |
|-------|--------|
| PVC `binding volumes: context deadline exceeded` | Hetzner CSI volume provisioning is slow through NAT. The playbook retries automatically. |
| Vault pods stuck in Pending | CSI driver needs time to provision volumes. Wait for CSI DaemonSet to be ready. |
| GitLab webservice not ready | Large images (~2GB) pull slowly through NAT bastion. Deployment has extended timeouts. |
| SSH tunnel drops | `run_tier.sh` auto-restores SSH tunnel. For manual runs, check bastion connectivity. |
| Helm timeout | All Helm installs use `wait: false` for slow environments. Pod readiness is checked separately. |

### Useful Commands

```bash
# Check cluster health
kubectl get nodes -o wide
kubectl get pods -A | grep -v Running | grep -v Completed

# Check specific namespace
kubectl get pods -n vault
kubectl get pods -n databases
kubectl get pods -n gitlab

# Vault status
kubectl exec -n vault vault-0 -- vault status

# PostgreSQL cluster status
kubectl get perconapgclusters -n databases

# MongoDB cluster status
kubectl get psmdb -n databases

# Check PVCs
kubectl get pvc -A
```

---

## GitLab EE License

GitLab EE Ultimate is deployed with an auto-generated license:

- **Edition**: Enterprise Edition Ultimate
- **Generated via**: `gitlab-license` Ruby gem (RSA 2048-bit key)
- **Valid until**: 2500-01-01
- **Users**: unlimited (500,000)
- **Features**: All Ultimate features enabled

The license is generated during deployment and stored in `playbooks/.gitlab-license/` (gitignored).

---

## Contributing

1. Fork the repository
2. Create a feature branch: `git checkout -b feature/my-feature`
3. Test with at least the `minimal` tier
4. Submit a pull request

---

## License

MIT
