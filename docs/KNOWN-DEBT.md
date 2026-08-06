# Known Technical Debt

Items that are intentionally not fixed because the fix carries more risk than
the debt. Each entry explains what is wrong, why it is not fixed, and what
would be required to resolve it.

## fun GitOps project path typo: `ansbile-k8s-full-setup-n0xeid`

**What:** The fun GitOps project at
`fun/argocd/ansbile-k8s-full-setup-n0xeid` has a typo: `ansbile` instead of
`ansible`.

**Why not fixed:** Renaming a GitLab project changes its path, which breaks
every Argo CD Application source URL that references it. Argo CD stores the
repository URL in the Application spec; a rename would cause all fun-games
Argo CD applications to fail reconciliation until every source URL is updated
and Argo CD is forced to refresh.

**Risk of fixing:** High. Requires coordinated downtime across all fun-games
Argo CD applications (preproduction, production, edge) plus a verified
rollback path.

**To resolve safely:**

1. Create a new project `fun/argocd/ansible-k8s-full-setup-n0xeid` (correct
   spelling).
2. Mirror or push all branches and tags.
3. Update every Argo CD Application source URL to point to the new path.
4. Verify all applications sync successfully.
5. Archive the old project.

This should only be done during a planned maintenance window.

## `fun/fun-games` outside convention (legacy repo)

**What:** Project `fun/fun-games` (id=1) sits directly under the `fun/` group
rather than under `fun/apps/` or `fun/development/`.

**Why not fixed:** This is the original repository created before the
convention was established. Active development has moved to
`fun/development/fun-games` (id=2). Moving or archiving the legacy repo risks
breaking historical CI references, Argo CD source URLs, and external clones.

## `agents/platform/steel-browser` vs `agents/apps/steel-browser`

**What:** Two steel-browser projects exist: `agents/platform/steel-browser`
(id=7) and `agents/apps/steel-browser` (id=37). The platform one is outside
the `agents/apps/` convention.

**Why not fixed:** The platform project holds the infrastructure/platform
component distinct from the application repo. Moving it risks breaking Argo CD
source URLs and build pipelines that reference it by path.
