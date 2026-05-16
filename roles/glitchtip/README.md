# glitchtip

Self-hosted GlitchTip — open-source, Sentry-compatible error tracking.

## Architecture

- **Web + Worker**: Helm chart `glitchtip/glitchtip`
- **Version**: GlitchTip app `v6.1.4`, Helm chart `8.2.0`
- **Database**: Reuses existing Percona PostgreSQL cluster (user: `glitchtip`, db: `glitchtip`)
- **Redis/Celery broker**: Reuses existing Dragonfly (DB 2)
- **Ingress**: Gateway API HTTPRoute via `main-gateway` (cilium-system)
- **TLS**: cert-manager with `letsencrypt-prod` ClusterIssuer
- **Email**: Optionally via Postal SMTP

## Access

- URL: `https://glitchtip.{{ domain }}`
- First user registers and becomes admin (if `glitchtip_enable_user_registration: true`)

## Tier scaling

| Tier | Web replicas | Dedicated worker | Web resources |
|------|--------------|------------------|---------------|
| minimal/small | 1 | Disabled | 100m/500m CPU, 384Mi/768Mi memory |
| medium/production | 2 | 2 replicas | 300m/1000m CPU, 768Mi/1536Mi memory |

Dedicated workers, when enabled, use 200m/1000m CPU and 512Mi/1Gi memory.

## Prerequisites

- `k8s-databases` role must create the `glitchtip` PG user (already configured)
- `dragonfly` role deployed
- `main-gateway` available in `cilium-system` namespace
- `cert-manager` + `letsencrypt-prod` ClusterIssuer
