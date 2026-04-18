# glitchtip

Self-hosted GlitchTip — open-source, Sentry-compatible error tracking.

## Architecture

- **Web + Worker**: Helm chart `glitchtip/glitchtip`
- **Database**: Reuses existing Percona PostgreSQL cluster (user: `glitchtip`, db: `glitchtip`)
- **Redis/Celery broker**: Reuses existing Dragonfly (DB 2)
- **Ingress**: Gateway API HTTPRoute via `main-gateway` (cilium-system)
- **TLS**: cert-manager with `letsencrypt-prod` ClusterIssuer
- **Email**: Optionally via Postal SMTP

## Access

- URL: `https://glitchtip.{{ domain }}`
- First user registers and becomes admin (if `glitchtip_enable_user_registration: true`)

## Tier scaling

| Tier | Web replicas | Worker replicas |
|------|-------------|-----------------|
| minimal/small | 1 | 1 |
| medium/production | 2 | 2 |

## Prerequisites

- `k8s-databases` role must create the `glitchtip` PG user (already configured)
- `dragonfly` role deployed
- `main-gateway` available in `cilium-system` namespace
- `cert-manager` + `letsencrypt-prod` ClusterIssuer
