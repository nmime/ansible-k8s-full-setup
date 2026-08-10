# Service & Environment Map

_Generated from the live cluster (`argocd` Applications, HTTPRoutes, Secrets) on
2026-08-10. All Argo Applications Synced + Healthy and **0 active alerts** unless
a row says otherwise._

## 1. Topology

```
                       Internet
                          │
                 95.217.170.241 (LB VIP 10.0.10.1)
                          │
              main-gateway (ns cilium-system, Gateway API)
                          │
   ┌──────────┬───────────┼────────────┬─────────────┬──────────────┐
   │          │           │            │             │              │
 fun-games  social-     gitlab      monitoring    storage        vault
 (3 envs)   agents     (git+reg)  (grafana/AM)  (seaweedfs s3)  (secrets)
   │        (3 envs)      │            │             │
   │        steel         │            │             │
   │        (2 envs)      │            │             │
   └──────────┴───────────┴─────┬──────┴─────────────┘
                                 │
            Shared datastores (ns databases):  MongoDB rs0 x3 ·
            PostgreSQL (percona) x3 + pgbouncer x2 · Dragonfly (redis) x2
                                 │
   Backups: velero (+node-agent) · Mail: postal · CI: gitlab runners (disabled)
```

## 2. Fun-Games (org `fun`) — all green

| Env | Namespace | Argo | Frontend | API | Bot webhook | Backend |
|-----|-----------|------|----------|-----|-------------|---------|
| production | `fun-games-production` | Synced/Healthy | `uno.funfiesta.games` `durak.funfiesta.games` | `api.uno.` `api.durak.` | `bot.uno.` `bot.durak.` | `backend.uno.` `backend.durak.` |
| preproduction | `fun-games-preproduction` | Synced/Healthy | `uno.pp.funfiesta.games` `durak.pp.funfiesta.games` | `api.uno.pp.` `api.durak.pp.` | `bot.uno.pp.` `bot.durak.pp.` | `backend.uno.pp.` `backend.durak.pp.` |
| edge | `fun-games-edge` | Synced/Healthy | — (edge tier mirrors nonprod) | — | — | — |

Per game (uno, durak): `frontend` :80, `api`, `bot`, `game-server` (StatefulSet),
`scheduler`, `autobots`, `admin`. Production adds CronJobs, HPAs, PDBs,
PrometheusRule/ServiceMonitor.

### Telegram bots (verified against live tokens via getMe)

| Env | UNO | Durak |
|-----|-----|-------|
| production | **@uno9bot** (6492027652) | **@Durak_Bot** (5067858351) |
| preproduction + edge | @uno9test_bot (7557830686) | @Durak_Test_Bot (7244061408) |

### Datastores used
- production: in-cluster MongoDB rs0 + Dragonfly (`rediss://dragonfly-tls.dragonfly:6380`)
- nonprod: external managed MongoDB `n0xeid-mongo.databases` + Dragonfly; `gameDatastores.enabled: false`

## 3. Social-Agents (org `agents`)

| Env | Namespace | Argo | Public host |
|-----|-----------|------|-------------|
| production | `social-agents-production` | Synced/Healthy | `social-agents.n0xeid.xyz` |
| preproduction | `social-agents-preproduction` | Synced/Healthy | `social-agents.pp.n0xeid.xyz` |
| edge | `social-agents-edge` | **OutOfSync/Degraded** | `social-agents` route (edge ns) |

Services: `social-agents-api`, `social-agents-web`, `social-agents-worker`,
`notification-scheduler`, `notification-consumer`, migrator (batch job).

> **edge caveat:** provisioned as a real tier (own namespace, values,
> external-secrets, resource-policy; gitops MR 63). It is blocked by two
> pre-existing shared dependencies, not by app code: (a) `datastore-client-ca`
> (copied live), and (b) edge vault secrets (`agents/social-agents/edge/*`,
> `agents/registry`) which return Vault 403 — the same dependency class the
> fun-games edge resolved with dedicated vault paths. Steel-browser has no edge
> tier. Resolve by adding the edge vault paths + re-running the secrets
> reconcile (vault root token is not reachable from the agent sandbox).

## 4. Steel (org `agents`, steel-browser)

| Env | Namespace | Argo | Public host |
|-----|-----------|------|-------------|
| production | `steel-production` | Synced/Healthy | — (internal) |
| preproduction | `steel-preproduction` | Synced/Healthy | — (internal) |

Services: `steel-scheduler`, `steel-worker`. Healthy; schedulers process
`/v1/scheduler/workers/heartbeat` continuously.

## 5. Dadya (org `dadya`)

**Placeholder org.** Namespaces `dadya-production`, `dadya-preproduction`,
`dadya-miner-edge` exist (scaffolded: quota/limitrange/default-deny netpol +
an AppProject) but contain **no workloads** — `projects/dadya/dadya-miner/base`
defines no Deployment/StatefulSet/Job and there is no dadya Argo Application.
Databases `dadya_prod` / `dadya_pp` are provisioned in Postgres (percona) and
their connection Secrets published to the dadya namespaces by the platform
`k8s-databases` role. The `dadya/apps/dadya-miner` repo (project 4) has a
`.helm` chart and `apps/` but is not yet onboarded into Argo.

## 6. Platform services

| Service | Namespace | Public host | Notes |
|---------|-----------|-------------|-------|
| Argo CD | argocd | `argocd.n0xeid.xyz` | root app + 10 apps, all Synced |
| GitLab | gitlab | `git.n0xeid.xyz` `registry.n0xeid.xyz` `kas.n0xeid.xyz` | self-hosted git + container registry |
| Grafana / Alertmanager | monitoring | `grafana.n0xeid.xyz` `alertmanager.n0xeid.xyz` | vmalertmanager-platform; **0 active alerts** |
| Vault | vault | `vault.n0xeid.xyz` | secrets backend for external-secrets |
| Coroot | coroot | `coroot.n0xeid.xyz` | observability |
| Umami | umami | `umami.n0xeid.xyz` | analytics |
| SeaweedFS (S3) | storage | `s3.n0xeid.xyz` `s3.funfiesta.games` `seaweedfs.n0xeid.xyz` | master x3, volume x3, filer |
| Postal (mail) | postal | `mail.n0xeid.xyz` `track.n0xeid.xyz` | transactional mail |
| nx-cache | nx-cache | `nx-cache.n0xeid.xyz` `nx-cache-dev.n0xeid.xyz` | build cache; dev tier requests 32Mi (worker-2 saturation) |
| Velero (backups) | velero | — | node-agent DaemonSet + controller |

## 7. Shared datastores (namespace `databases`)

- **MongoDB**: `n0xeid-medium-optimized-cx-mongo-rs0` x3 (replica set), percona-server-mongodb-operator
- **PostgreSQL**: `n0xeid-medium-optimized-cx-pg-instance1` x3 + pgbouncer x2 + repo-host, percona-pg-operator
- **Dragonfly** (Redis-compatible, ns `dragonfly`): `dragonfly-0`, `dragonfly-1` (TLS :6380)

## 8. CI (GitLab runners) — intentionally disabled

Dedicated CI workers (indexes 4/5) are decommissioned from the medium-optimized
topology, so no node carries `workload.n0xeid.xyz/ci-{general,build,docker}`
labels. All three runner pools (general, image_builder, docker_host) are set to
`enabled: false` in `platform-orchestrator/profiles/medium-optimized.yaml`
(platform repo MR 5) and their Deployments scaled to 0 — clearing
`GitLabRunnerMetricsDown` / `DeploymentReplicasUnavailable`. **Re-enable these
flags together with re-provisioning the dedicated CI workers to restore CI.**
