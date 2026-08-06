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
