# k8s-databases

Deploys Percona PostgreSQL (with PgBouncer and pgBackRest) and optionally
Percona MongoDB (with PBM) into the `databases` namespace.

## Key variables

- `db_ns` — target namespace (default: `databases`)
- Tier-specific replica counts, storage sizes, and resource requests are set
  dynamically from the active profile.

## Where applied

Included by `playbooks/deploy_platform.yml`. PostgreSQL is mandatory for
`small` and larger profiles; MongoDB is an explicit opt-in.
