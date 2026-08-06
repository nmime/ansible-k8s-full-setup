# k8s-gitops

Deploys Argo CD with constrained projects, sources, and resource permissions.
Supports optional HA mode with redundant server/repo/controller pods,
Redis/Sentinel, and HAProxy endpoints.

## Key variables

- `gitops.ha_enabled` — enables HA topology when true

## Where applied

Included by `playbooks/deploy_platform.yml` for `small` and larger profiles.
