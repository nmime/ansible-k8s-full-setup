# Infrastructure Map — n0xeid Cluster

> **Generated:** 2026-08-06 · **Source:** Live `kubectl` audit against controller-admin kubeconfig (`K=/tmp/kubeconfig-admin-direct.yaml`), GitLab API, Hetzner metadata.
> **Cluster:** Kubernetes v1.35.4 · 9 nodes · Hetzner Cloud `hel1-dc2` · CNI: Cilium · Ingress: Cilium Gateway API + MetalLB FRR

---

## 1. Network Topology

```
                                    ┌─────────────────────────────────┐
                                    │         INTERNET                │
                                    └────────┬───────────┬────────────┘
                                             │           │
                            ┌────────────────┘           └─────────────────┐
                            │ 80/443 (HTTP/S)              25/587 (SMTP)   │
                            ▼                                              ▼
               ┌────────────────────────────┐                ┌──────────────────────┐
               │  MetalLB Pool (FRR BGP)    │                │  postal-smtp LB      │
               │  10.0.10.1 – 10.0.10.254   │                │  10.0.10.3 (:25,:587)│
               └─────┬──────────────┬───────┘                └──────────────────────┘
                     │              │
          10.0.10.1  │              │  10.0.10.2
                     ▼              ▼
    ┌──────────────────────┐   ┌──────────────────────┐
    │  cilium/main-gateway │   │ cilium/admin-gateway │
    │  (PUBLIC traffic)    │   │ (ADMIN / VPN traffic)│
    │  ports: 80,443       │   │ ports: 443,8080,8443 │
    └──────────┬───────────┘   └──────────┬───────────┘
               │                          │
               │   ┌──────────────────────┤  VPN: headscale
               │   │                      │  (vpn.n0xeid.xyz)
               │   │                      │  tailnet mesh →
               │   │                      │  admin-gateway:443
               │   │                      │
               ▼   ▼                      ▼
    ┌──────────────────────────────────────────────────────────────────┐
    │                    KUBERNETES POD NETWORK                        │
    │                    10.233.0.0/16 (ClusterIP)                     │
    │                    10.0.2.0/24   (Node network)                  │
    │                                                                  │
    │   ┌─────────── KUBE-API ───────────┐                             │
    │   │  https://10.0.2.4:6443         │  (master-3, VIP via keepalived│
    │   │  3× apiserver (HA)             │   /kube-vip)                 │
    │   └────────────────────────────────┘                             │
    └──────────────────────────────────────────────────────────────────┘
```

### Tailnet / VPN overlay

```
  ┌──────────────┐      WireGuard      ┌─────────────────────┐
  │  Admin Laptop│◄───────────────────►│  Headscale          │
  │  (Mac)       │    tailnet          │  vpn.n0xeid.xyz     │
  │  100.64.0.11 │                     │  (control plane)    │
  └──────┬───────┘                     └─────────────────────┘
         │
         │ tailnet route → 10.0.10.2 (admin-gateway)
         │                → 10.0.2.4:6443 (kube-api)
         │
         ▼
  Admin-gateway HTTPRoutes (VPN-only):
    argocd.n0xeid.xyz · grafana.n0xeid.xyz · vault.n0xeid.xyz
    coroot.n0xeid.xyz · alertmanager.n0xeid.xyz
    mail.n0xeid.xyz · metabase.n0xeid.xyz
    seaweedfs.n0xeid.xyz · s3.n0xeid.xyz
    umami.n0xeid.xyz (dashboard)
```

---

## 2. Cluster Nodes

All nodes in **Hetzner `hel1-dc2`** (single data centre — no multi-AZ).

```
 CONTROL PLANE (cx33 · 4 vCPU / 8 GiB each)          WORKERS (mixed)
 ┌─────────────────────────────────┐                 ┌──────────────────────────────────────────┐
 │ master-1   10.0.2.2   cx33      │                 │ worker-1   10.0.2.5   cx43  8vCPU/16GiB  │
 │ master-2   10.0.2.3   cx33      │                 │ worker-2   10.0.2.6   cx43  8vCPU/16GiB  │
 │ master-3   10.0.2.4   cx33 ◄API │                 │ worker-3   10.0.2.7   cx43  8vCPU/16GiB  │
 └─────────────────────────────────┘                 │ worker-4   10.0.2.13  cx43  8vCPU/16GiB  │
   • etcd (3-node quorum)                            │ worker-5   10.0.2.14  cx43  8vCPU/16GiB  │
   • kube-apiserver (HA, restarts 3–6)               │ worker-6   10.0.2.11  cx33  4vCPU/8GiB   │
   • kube-scheduler ⚠ (restarts 34–38)               └──────────────────────────────────────────┘
   • kube-controller-manager                         • Cilium DS + hcloud-csi-node DS on all
   • cilium DS                                       • Local PVs (platform-local) on masters
```

> ⚠ **kube-scheduler restarts 34/38/34** on master-1/2/3 — known issue (see KNOWN-DEBT / RUNBOOK).

### kube-system control-plane components

| Component | Type | Notes |
|---|---|---|
| kube-apiserver | static pod ×3 | HA via kube-vip VIP on 10.0.2.4 |
| kube-scheduler | static pod ×3 | ⚠ High restart count (34–38) |
| kube-controller-manager | static pod ×3 | 0 restarts |
| etcd | static pod ×3 | 3-node quorum |
| **Cilium** | DaemonSet | CNI + Gateway API controller + Envoy DS + Hubble (relay + UI) |
| **hcloud-ccm** | Deployment | Hetzner Cloud Controller Manager |
| **hcloud-csi** | Controller Deployment + Node DS | Provisioner for `hcloud-volumes` StorageClass |
| **CoreDNS** | Deployment (2 pods) | + nodelocaldns DaemonSet + dns-autoscaler |
| metrics-server | Deployment | HPA resource metrics |
| dns-autoscaler | Deployment | (NOT cluster-autoscaler — nodes are static) |

---

## 3. Gateway / HTTPRoute Map

Two Cilium Gateways front all cluster ingress (GatewayClass: `cilium`).

### 3a. main-gateway (10.0.10.1) — PUBLIC

```
  Internet ──80/443──►  cilium/main-gateway  ──►  HTTPRoutes:

  funfiesta.games  (prod):
    *.funfiesta.games          → uno/durak frontend, api, admin, bot
    backend.uno.funfiesta.games → uno game-server
    s3.funfiesta.games         → seaweedfs-s3 (funfiesta bucket)

  funfiesta.games  (preprod):
    *.pp.funfiesta.games       → uno/durak (same layout, pp prefix)

  social-agents:
    social-agents.n0xeid.xyz       → social-agents-prod (api/web/worker)
    social-agents.pp.n0xeid.xyz    → social-agents-preprod

  git.n0xeid.xyz (public clone/fetch):
    gitlab-webservice-public       → gitlab-webservice-default
    gitlab-registry-public         → gitlab-registry (pull images)
    gitlab-registry-auth-public    → registry auth

  other public:
    umami.n0xeid.xyz (ingest)      → umami analytics
    track.n0xeid.xyz               → postal-web (tracking pixel)
    nx-cache.n0xeid.xyz            → nx-cache-protected
    nx-cache-dev.n0xeid.xyz        → nx-cache-development

> **Note (2026-08-10):** `nx-cache-development` runs with `requests.memory: 32Mi`
> (down from 128Mi). Its PV is pinned to `worker-2`, which runs at ~97-99%
> memory-request saturation; 64Mi+ requests no longer schedule there. The dev
> cache tolerates the lower reservation (limits remain 512Mi). The StatefulSet
> is currently **not** managed by any Argo Application (the former
> `shared-nx-cache` app was removed); the reduced request was applied directly
> and survives restarts. If it is re-onboarded into GitOps, keep
> `requests.memory <= 32Mi` or add capacity to worker-2.
```

### 3b. admin-gateway (10.0.10.2) — ADMIN / VPN

```
  VPN/Tailnet ──443──►  cilium/admin-gateway  ──►  HTTPRoutes (restricted):

  Platform ops:
    argocd.n0xeid.xyz     → argocd-server (GitOps UI)
    grafana.n0xeid.xyz    → grafana (dashboards)
    alertmanager.n0xeid.xyz → vmalertmanager-platform
    coroot.n0xeid.xyz     → coroot-coroot (APM)
    vault.n0xeid.xyz      → vault-active (secrets)

  GitLab admin:
    git.n0xeid.xyz / gitlab.n0xeid.xyz → gitlab-webservice (admin console)
    registry.n0xeid.xyz                → gitlab-registry
    kas.n0xeid.xyz                     → gitlab-kas (Kubernetes agent)

  Data & ops tools:
    metabase.n0xeid.xyz   → metabase (BI dashboards)
    mail.n0xeid.xyz       → postal-web (mail admin)
    seaweedfs.n0xeid.xyz  → seaweedfs console
    s3.n0xeid.xyz         → seaweedfs-s3 API
    umami.n0xeid.xyz      → umami dashboard
```

### 3c. Non-HTTP LoadBalancers

| Service | LB IP | Ports | Purpose |
|---|---|---|---|
| postal-smtp | 10.0.10.3 | 25, 587 | Inbound/outbound mail |

---

## 4. Component Map by Namespace

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                            PLATFORM LAYER                                   │
├─────────────────┬───────────────────────────────────────────────────────────┤
│ kube-system     │ cilium, cilium-envoy, hubble-ui/relay, coredns,           │
│                 │ nodelocaldns, dns-autoscaler, hcloud-ccm, hcloud-csi,     │
│                 │ metrics-server                                            │
│ metallb-system  │ metallb-controller, frr-k8s (BGP L2/L3 LB for 10.0.10.x)  │
│ cilium-system   │ cilium gateway instances (main + admin)                   │
│ cert-manager    │ cert-manager + cainjector + webhook + hetzner-webhook      │
│                 │ (ACME DNS-01 via Hetzner DNS API)                         │
│ external-secrets│ external-secrets controller + webhook (pulls from Vault)  │
│ vault           │ HashiCorp Vault HA (3 pods, Raft, active/standby)         │
│ keda            │ keda-operator + metrics-apiserver + admission-webhook     │
│                 │ (event-driven autoscaling — fun-games game-servers)       │
│ velero          │ velero server (backups → S3 bucket n0xeid-dr-20260801)    │
│ argocd          │ argocd-server, repo-server, redis, applicationset-ctrl    │
│                 │ (GitOps — syncs agents/argocd/ansible-k8s-full-setup-n0xeid)│
│ dragonfly-operator│ dragonfly-operator controller-manager                    │
│ storage         │ SeaweedFS (master×3, volume×3, filer, S3)                 │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ gitlab          │ GitLab: webservice, gitaly, sidekiq, registry, shell(SSH),│
│                 │ kas, gitlab-exporter, redis(pg)                           │
│ postal          │ Postal mail server: web, smtp(LB), mariadb                │
│ coroot          │ Coroot APM: coroot-server, clickhouse, clickhouse-keeper  │
├─────────────────┼───────────────────────────────────────────────────────────┤
│                            MONITORING LAYER                                 │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ monitoring      │ VictoriaMetrics: vmagent, vminsert, vmselect, vmstorage,  │
│                 │   vmalert, vmalertmanager (×2 HA), vm-operator            │
│                 │ Grafana (dashboards)                                      │
│                 │ Loki (logs) + loki-gateway + canary + memberlist          │
│                 │ otel-collector (traces/metrics pipeline)                   │
│                 │ blackbox-exporter (synthetic probes)                       │
│                 │ hetzner-cloud-exporter                                    │
│                 │ bastion-node-exporter (external node metrics)             │
│                 │ ha-egress-health                                          │
├─────────────────┼───────────────────────────────────────────────────────────┤
│                              DATA LAYER                                     │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ databases       │ PostgreSQL (Percona Operator): pg-ha (primary+replicas),  │
│                 │   pgbouncer, 3 instances ×30Gi                            │
│                 │   n0xeid-pg (standalone)                                  │
│                 │ MongoDB: n0xeid-mongo (replica-set rs0, 3 ×20Gi)          │
│ dragonfly       │ Dragonfly (Redis-compatible): 2 replicas ×10Gi (shared)   │
│ postal          │ MariaDB (postal-mariadb ×20Gi)                            │
│ storage         │ SeaweedFS S3 / object storage (master×3, volume×3, filer) │
│ coroot          │ ClickHouse (keeper×2 + shard×1)                           │
├─────────────────┼───────────────────────────────────────────────────────────┤
│                          APPLICATION LAYER                                  │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ fun-games-      │ UNO: frontend, api, admin, bot, game-server(+router+      │
│   production    │   headless), scheduler, autobots                          │
│                 │ Durak: frontend, api, admin, bot, game-server(+router+     │
│                 │   headless), scheduler, autobots                          │
│                 │ Dragonfly cache ×2                                        │
│ fun-games-      │ Same as production (preprod environment)                   │
│   preproduction │                                                           │
│ social-agents-  │ social-agents: api, web, worker + steel-scheduler/worker  │
│   production    │                                                           │
│ social-agents-  │ Same (preprod)                                            │
│   preproduction │                                                           │
│ agents          │ steel-scheduler, steel-worker                             │
│ umami           │ umami (web analytics)                                     │
│ analytics       │ metabase (BI)                                             │
│ nx-cache        │ nx-cache-development, nx-cache-protected, nx-cache-gateway│
│ dadya-prod/     │ (placeholder namespaces — no pods currently)              │
│   preprod       │                                                           │
├─────────────────┼───────────────────────────────────────────────────────────┤
│                              CI LAYER                                       │
├─────────────────┼───────────────────────────────────────────────────────────┤
│ gitlab-ci-      │ gitlab-runner ×2 (tag: none, run-untagged)                │
│   general       │ Runner #107 (n0xeid-general-ci)                           │
│ gitlab-image-   │ gitlab-image-builder-runner ×1 (tag: image-build)         │
│   builds        │ Runner #39                                                │
│ gitlab-docker-  │ gitlab-docker-host-runner ×1 (tag: docker-host)           │
│   builds        │ Runner #40                                                │
│ gitlab-ci-      │ (cleanup namespace — no active pods)                      │
│   cleanup       │                                                           │
└─────────────────┴───────────────────────────────────────────────────────────┘
```

### Empty / placeholder namespaces (no pods)

`default` · `production` · `elasticsearch` · `temporal` · `glitchtip` · `backups` · `dadya-production` · `dadya-preproduction` · `fun` · `gateway-secrets` · `cilium-secrets` · `dragonfly-operator-system`

---

## 5. Storage Map

### Storage Classes

| Class | Provisioner | Reclaim | Binding | Expansion |
|---|---|---|---|---|
| `hcloud-volumes` (default) | csi.hetzner.cloud | Delete | WaitForFirstConsumer | ✅ |
| `platform-local` | kubernetes.io/no-provisioner | Retain | WaitForFirstConsumer | ❌ |

### PVC Inventory

```
  NAMESPACE                  PVC COUNT   TOTAL SIZE   STORAGE CLASS
  ─────────────────────────  ─────────   ──────────   ─────────────
  databases (Postgres)       3           90 Gi        platform-local (30Gi ×3)
  databases (Mongo)          3           60 Gi        platform-local (20Gi ×3)
  databases (pg-repo)        1           10 Gi        hcloud-volumes
  storage (SeaweedFS)        10          126 Gi       platform-local + hcloud
  monitoring (vmstorage)     1           40 Gi        hcloud-volumes
  gitlab (gitaly)            1           30 Gi        hcloud-volumes
  coroot (clickhouse)        5           60 Gi        hcloud-volumes
  vault (data + audit)       6           60 Gi        platform-local + hcloud
  postal (mariadb)           1           20 Gi        hcloud-volumes
  dragonfly (shared)         2           20 Gi        hcloud-volumes
  fun-games-prod (cache)     2           20 Gi        hcloud-volumes
  fun-games-pp (cache)       2           20 Gi        hcloud-volumes
  monitoring (grafana)       1           10 Gi        hcloud-volumes
  monitoring (loki)          1           10 Gi        hcloud-volumes
  monitoring (alertmanager)  2           10 Gi        hcloud-volumes
  nx-cache                   2           20 Gi        platform-local
  ─────────────────────────  ─────────   ──────────
  TOTAL                      42 PVCs     ~596 Gi
```

### Velero Backup

| Item | Detail |
|---|---|
| BackupStorageLocation | `default` (provider: aws, bucket: `n0xeid-dr-20260801`, phase: Available) |
| Schedule | `velero-full-cluster` — daily at 02:30 UTC |
| Last backup | 2026-08-06T02:30:49Z |

---

## 6. Traffic Flow Examples

### 6a. End-user → Fun-Games (UNO)

```
  User browser
      │
      ▼  DNS: uno.funfiesta.games → MetalLB public IP
  cilium/main-gateway (10.0.10.1:443)
      │  TLS termination (cert-manager ACME cert)
      ▼  HTTPRoute: uno-frontend (fun-games-production)
  fun-games-uno-frontend (nginx, :80)
      │
      ├──► static assets served
      │
      ▼  API calls
  fun-games-uno-api (:5001)
      │
      ├──► fun-games-uno-game-server (:5000) — WebSocket game state
      │       (scaled by KEDA on queue depth)
      │       ├──► fun-games-cache (Dragonfly :6379)
      │       └──► n0xeid-pg (PostgreSQL :5432)
      │
      └──► fun-games-uno-bot (:5002) — Telegram bot interface
```

### 6b. Developer → GitLab (push/pull over VPN)

```
  Developer on tailnet (100.64.0.x)
      │
      ├──► git push over SSH:
      │    git.n0xeid.xyz:22 → gitlab-gitlab-shell → gitaly (repo storage)
      │
      └──► git push over HTTPS (web console):
           git.n0xeid.xyz:443
               │
               ▼  VPN route → admin-gateway (10.0.10.2)
           cilium/admin-gateway
               │  HTTPRoute: gitlab-webservice
               ▼
           gitlab-webservice-default (:8080)
               │
               ▼
           gitlab-gitaly (:8075) — 30Gi PVC (hcloud-volumes)

  CI pipeline triggers →
      ├──► Runner #107 (general-ci, untagged)     → gitlab-ci-general pods
      ├──► Runner #39 (image-build)                → gitlab-image-builds pod (kaniko)
      └──► Runner #40 (docker-host)                → gitlab-docker-builds pod (DinD)
```

### 6c. Admin → ArgoCD (VPN-only)

```
  Admin on tailnet
      │
      ▼  DNS: argocd.n0xeid.xyz → 10.0.10.2 (admin-gateway, VPN-routed)
  cilium/admin-gateway (:443)
      │  HTTPRoute: argocd-server
      ▼
  argocd-server (:80,:443)
      │
      ▼  polls/syncs
  argocd-repo-server (:8081)
      │  fetches manifests from
      ▼
  GitLab: agents/argocd/ansible-k8s-full-setup-n0xeid
      │  (the live-cluster GitOps source-of-truth)
      ▼
  Applies manifests → all app/platform namespaces
```

---

## 7. Priority / Scheduling Classes (for reference)

| PriorityClass | Value | Purpose |
|---|---|---|
| system-node-critical | 2,000,001,000 | kube-system absolute priority |
| system-cluster-critical | 2,000,000,000 | kube-system cluster priority |
| k8s-cluster-critical | 1,000,000,000 | cluster-wide critical |
| n0xeid-platform-critical | 2,000,000 | platform services |
| n0xeid-service-production | 100,000 | prod app services |
| platform-ci / app-ci-high | 100,000 / 1,000,000 | CI pipelines |
| n0xeid-service-environment | 50,000 | env-level services |
| n0xeid-service-review | 10,000 | review environments |
| n0xeid-ci-production | -100 | CI prod (batch, preemptible) |
| n0xeid-cache | -10 | Dragonfly caches |
| n0xeid-batch | -500 | batch workloads |

---

*End of infrastructure map.*
