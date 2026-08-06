# temporal

Deploys Temporal server, UI, and admin tools for workflow orchestration.

## Key variables

- `temporal_ns` — target namespace (default: `temporal`)

## Where applied

Included by `playbooks/deploy_platform.yml` when `temporal` is explicitly
opted in. Requires PostgreSQL and Dragonfly.
