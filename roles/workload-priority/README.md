# workload-priority

Creates Kubernetes PriorityClasses and applies them so critical workloads are
scheduled before best-effort work. Variables are defined centrally in
`defaults/main.yml` so PriorityClasses can be created before workload charts
and applied after all controllers exist.

## Key variables

- `workload_priority_rollout_timeout` — rollout wait (default: `10m`)

## Where applied

Included by `playbooks/deploy_platform.yml` as part of the platform baseline.
