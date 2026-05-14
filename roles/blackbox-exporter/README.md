# Blackbox Exporter

Synthetic/uptime monitoring — probes public HTTPS endpoints and internal TCP services.

## What it does
- Deploys `prom/blackbox-exporter` via Helm in the `monitoring` namespace
- Creates **VMProbe** CRs (VictoriaMetrics operator native — no Prometheus needed)
- Probes public platform URLs (grafana, gitlab, argocd, mail, temporal, kibana, glitchtip, and Daytona when enabled) every 60s
- Probes internal TCP (PostgreSQL, Elasticsearch, Dragonfly, MinIO) every 60s
- Exposes `probe_success`, `probe_http_status_code`, `probe_ssl_earliest_cert_expiry`, `probe_duration_seconds`

## Alerts
Cert-expiry + probe-down alerts are defined in `roles/k8s-observability` VMRules.

## Toggles
- `deploy_blackbox: true` (default)
- `deploy_daytona: true` adds the Daytona public probe target
