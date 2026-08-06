# k8s-autoscaling

Deploys KEDA for event-driven workload autoscaling.

## Key variables

- `keda_namespace` — target namespace (default: `keda`)

## Where applied

Included by `playbooks/deploy_platform.yml` when the autoscaling component is
selected by the active profile.
