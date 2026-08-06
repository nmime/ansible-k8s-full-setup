# k8s-observability

Deploys the observability stack: VictoriaMetrics, Grafana, Loki/Promtail or
ELK/EFK collectors, PMM, Alertmanager, and optionally Tempo/OTel and Coroot.

## Key variables

- `monitoring_namespace` — target namespace (default: `monitoring`)
- Tier-specific retention, replica, and resource settings are set from the
  active profile.

## Where applied

Included by `playbooks/deploy_platform.yml` for `small` and larger profiles.
See `docs/OBSERVABILITY.md`.
