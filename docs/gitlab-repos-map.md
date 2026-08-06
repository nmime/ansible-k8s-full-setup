# GitLab Repos Map — git.n0xeid.xyz

> **Generated:** 2026-08-06 · **Source:** GitLab API v4 (`/api/v4/groups`, `/api/v4/projects`, `/api/v4/runners`) — live data.

---

## 1. Group Tree

```
git.n0xeid.xyz
│
├── platform/                         (ID 145)  Shared platform infrastructure
│   └── ansible-k8s-full-setup        (ID 103)  ★ SOURCE OF TRUTH — base infra / Ansible / kubespray
│
├── fun/                              (ID 2)    Fun Games division
│   ├── apps/                         (ID 43)   [empty — no projects yet]
│   ├── argocd/                       (ID 7)
│   │   └── ansbile-k8s-full-setup-n0xeid (ID 3)  ⚠ LEGACY — old GitOps, superseded
│   ├── development/                  (ID 6)
│   │   └── fun-games                 (ID 2)    UNO + Durak app source (active dev)
│   ├── devops/                       (ID 8)    [empty — no projects yet]
│   ├── gitops/                       (ID 45)   [legacy/unsorted — no projects yet]
│   ├── platform/                     (ID 44)   [empty]
│   ├── team/                         (ID 9)
│   │   ├── fun-games-developers      (ID 10)   [access group — humans]
│   │   └── fun-games-maintainers     (ID 11)   [access group — humans]
│   ├── tools/                        (ID 46)   [empty]
│   ├── acl/                          (ID 12)   External service authorization groups
│   │   └── k8s/                      (ID 13)
│   │       └── main/                 (ID 14)   Main cluster authorization
│   │           ├── prod/             (ID 15)
│   │           │   ├── fun-games-prod-ro  (ID 17)  [robot: read-only prod]
│   │           │   └── fun-games-prod-rw  (ID 18)  [robot: read-write prod]
│   │           └── pp/               (ID 16)
│   │               ├── fun-games-pp-ro    (ID 19)  [robot: read-only preprod]
│   │               └── fun-games-pp-rw    (ID 20)  [robot: read-write preprod]
│   │
│   ├── fun-games                     (ID 1)    ⚠ LEGACY — original repo (pre-restructure)
│   │
│   ...top-level
│
├── agents/                           (ID 31)   Agents division: AI agents + platform
│   ├── acl/                          (ID 35)   [access control]
│   ├── apps/                         (ID 32)
│   │   ├── social-agents             (ID 6)    AI social-agent application source
│   │   └── steel-browser             (ID 37)   Steel browser application
│   ├── argocd/                       (ID 109)
│   │   └── ansible-k8s-full-setup-n0xeid (ID 70)  ★ SOURCE OF TRUTH — live cluster GitOps
│   ├── devops/                       (ID 36)   [CI / delivery tooling]
│   ├── gitops/                       (ID 34)
│   │   └── cluster                   (ID 8)    Agents cluster GitOps config
│   ├── platform/                     (ID 33)
│   │   └── steel-browser             (ID 7)    Steel browser platform/infra component
│   ├── team/                         (ID 37)   [team/access mgmt]
│   └── tools/                        (ID 48)   [tooling/automation]
│
└── dadya/                            (ID 24)   Dadya division: mining
    ├── acl/                          (ID 28)   [access control]
    ├── apps/                         (ID 25)
    │   └── dadya-miner               (ID 4)    Mining application source
    ├── devops/                       (ID 29)   [CI / delivery tooling]
    ├── gitops/                       (ID 27)
    │   └── cluster                   (ID 5)    Dadya cluster GitOps config
    ├── platform/                     (ID 26)   [platform infra]
    ├── team/                         (ID 30)   [team/access mgmt]
    └── tools/                        (ID 47)   [tooling/automation]
```

---

## 2. All Projects (Complete Inventory)

| ID | Path | Purpose | Branch | Visibility |
|---|---|---|---|---|
| 103 | **platform/ansible-k8s-full-setup** | ★ Base infra: Ansible + kubespray full K8s setup, roles, playbooks, docs | main | private |
| 70 | **agents/argocd/ansible-k8s-full-setup-n0xeid** | ★ Live cluster GitOps — Argo CD ApplicationSet source | main | private |
| 2 | fun/development/fun-games | UNO + Durak app source, CI/CD, container images | main | private |
| 1 | ⚠ fun/fun-games | Legacy/original repo (pre-restructure) | main | private |
| 3 | ⚠ fun/argocd/ansbile-k8s-full-setup-n0xeid | Old GitOps config — superseded by agents/argocd/#70 | main | private |
| 6 | agents/apps/social-agents | AI social-agent application source | main | private |
| 37 | agents/apps/steel-browser | Steel browser application | main | private |
| 7 | agents/platform/steel-browser | Steel browser platform/infrastructure component | main | private |
| 4 | dadya/apps/dadya-miner | Mining application source | main | private |
| 8 | agents/gitops/cluster | Agents cluster GitOps configuration | main | private |
| 5 | dadya/gitops/cluster | Dadya cluster GitOps configuration | main | private |

> **Total:** 11 projects across 4 top-level groups.

---

## 3. Source-of-Truth Repos

```
  ┌─────────────────────────────────────────────────────────────────┐
  │  SOURCE OF TRUTH #1 — Base Infrastructure                       │
  │                                                                 │
  │  platform/ansible-k8s-full-setup  (GitLab ID 103)              │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ gitlab:  git.n0xeid.xyz/platform/ansible-k8s-full-setup │   │
  │  │ origin:  github.com/nmime/ansible-k8s-full-setup (mirror)│  │
  │  │ role:    Ansible roles, kubespray, cluster bootstrap,    │   │
  │  │          base docs (this file lives here)                │   │
  │  └─────────────────────────────────────────────────────────┘   │
  │                                                                 │
  │  SOURCE OF TRUTH #2 — Live Cluster GitOps                      │
  │                                                                 │
  │  agents/argocd/ansible-k8s-full-setup-n0xeid  (GitLab ID 70)  │
  │  ┌─────────────────────────────────────────────────────────┐   │
  │  │ origin:   git.n0xeid.xyz/agents/argocd/...n0xeid        │   │
  │  │ upstream: github.com/nmime/ansible-k8s-full-setup (mirror)│  │
  │  │ role:    Argo CD syncs this → applies to live cluster    │   │
  │  │          (all app/platform namespaces)                   │   │
  │  └─────────────────────────────────────────────────────────┘   │
  └─────────────────────────────────────────────────────────────────┘
```

**These two are the ONLY repos to commit to** (per governance rules).

### Git remotes (verified)

```
  platform/ansible-k8s-full-setup (local: /Users/nmi/IT/Projects/ansible-k8s-full-setup)
    gitlab  → git.n0xeid.xyz/platform/ansible-k8s-full-setup.git
    origin  → github.com/nmime/ansible-k8s-full-setup.git        ← GitHub mirror

  agents/argocd/ansible-k8s-full-setup-n0xeid (local: /Users/nmi/.../ansible-k8s-full-setup-n0xeid)
    origin    → git.n0xeid.xyz/agents/argocd/ansible-k8s-full-setup-n0xeid.git
    upstream  → github.com/nmime/ansible-k8s-full-setup.git      ← GitHub mirror
```

---

## 4. Legacy / Duplicate Repos

| Project | ID | Status | Reason |
|---|---|---|---|
| `fun/fun-games` | 1 | ⚠ **LEGACY** | Original Fun Games repo before restructuring into `fun/development/`. Active development has moved to ID 2. Retain for history; do not push new work here. |
| `fun/argocd/ansbile-k8s-full-setup-n0xeid` | 3 | ⚠ **LEGACY / DUPLICATE** | Old GitOps config (note typo: "ansbile"). Superseded by `agents/argocd/ansible-k8s-full-setup-n0xeid` (ID 70). Argo CD no longer syncs this. Safe to archive after verifying no ApplicationSet references it. |

### Restructuring pattern

```
  BEFORE (legacy):                    AFTER (current):
  ┌──────────────────┐                ┌─────────────────────────────┐
  │ fun/fun-games #1 │  ──split──►    │ fun/development/fun-games #2│  (app source)
  │ (monorepo)       │                └─────────────────────────────┘
  └──────────────────┘                ┌─────────────────────────────┐
                                      │ platform/ansible-k8s...#103 │  (base infra)
                                      └─────────────────────────────┘
                                      ┌─────────────────────────────┐
                                      │ agents/argocd/...n0xeid #70 │  (GitOps)
                                      └─────────────────────────────┘

  fun/argocd/ansbile-... #3 ──superseded──► agents/argocd/...n0xeid #70
```

---

## 5. Runner Map

Three shared runners registered in GitLab, deployed as Kubernetes workloads.

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │                         GITLAB RUNNERS                               │
  ├──────────┬──────────────┬──────────────┬────────────┬────────────────┤
  │ Runner ID│ Description  │ Tag          │ K8s NS     │ Pod(s)         │
  ├──────────┼──────────────┼──────────────┼────────────┼────────────────┤
  │ 107      │ n0xeid-      │ (none)       │ gitlab-ci- │ gitlab-runner  │
  │          │ general-ci   │ run-untagged │ general    │ ×2 replicas    │
  ├──────────┼──────────────┼──────────────┼────────────┼────────────────┤
  │ 39       │ ansible-k8s- │ image-build  │ gitlab-    │ gitlab-image-  │
  │          │ protected-   │              │ image-     │ builder-runner │
  │          │ image-builder│              │ builds     │ ×1             │
  ├──────────┼──────────────┼──────────────┼────────────┼────────────────┤
  │ 40       │ ansible-k8s- │ docker-host  │ gitlab-    │ gitlab-docker- │
  │          │ protected-   │              │ docker-    │ host-runner    │
  │          │ docker-host  │              │ builds     │ ×1             │
  └──────────┴──────────────┴──────────────┴────────────┴────────────────┘
```

### Runner → Project → Tag routing

```
  CI Job in .gitlab-ci.yml
      │
      ├── tags: [] (untagged) ──────► Runner #107 (general-ci)
      │                                   ├── fun/development/fun-games
      │                                   ├── agents/apps/social-agents
      │                                   ├── agents/apps/steel-browser
      │                                   ├── dadya/apps/dadya-miner
      │                                   └── any project with untagged jobs
      │
      ├── tags: [image-build] ──────► Runner #39 (image-builder)
      │                                   ├── kaniko build jobs
      │                                   └── (platform/agents/fun image builds)
      │
      └── tags: [docker-host] ──────► Runner #40 (docker-host)
                                          ├── Docker-in-Docker build jobs
                                          └── (privileged container builds)
```

| Property | #107 general | #39 image-builder | #40 docker-host |
|---|---|---|---|
| Shared | ✅ | ✅ | ✅ |
| Runs untagged | ✅ | ❌ | ❌ |
| Access level | not_protected | ref_protected | ref_protected |
| Locked | ❌ | ❌ | ❌ |
| Status | online | online | online |

> **Note:** No GitLab-side mirrors are configured (`mirror=false` for all projects). The GitHub mirrors (`github.com/nmime/ansible-k8s-full-setup`) are maintained via local git remotes + manual push, not GitLab's mirror feature.

---

## 6. Division Summary

```
  ┌───────────┬────────────┬──────────────┬──────────────────────────────────┐
  │ Division  │ Groups     │ Projects    │ Key repos                        │
  ├───────────┼────────────┼──────────────┼──────────────────────────────────┤
  │ platform  │ 1 (+0 sub) │ 1           │ ansible-k8s-full-setup ★         │
  │ fun       │ 1 + 12 sub │ 2 (1 legacy)│ fun-games (dev), fun-games (leg) │
  │ agents    │ 1 + 8 sub  │ 5           │ ansible-k8s...n0xeid ★,          │
  │           │            │             │ social-agents, steel-browser ×2, │
  │           │            │             │ gitops/cluster                   │
  │ dadya     │ 1 + 7 sub  │ 2           │ dadya-miner, gitops/cluster      │
  ├───────────┼────────────┼──────────────┼──────────────────────────────────┤
  │ TOTAL     │ 4 + 27 sub │ 11          │ 2 source-of-truth + 2 legacy     │
  └───────────┴────────────┴──────────────┴──────────────────────────────────┘
```

---

*End of GitLab repos map.*
