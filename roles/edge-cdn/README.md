# edge-cdn

Manages the explicit multi-region edge CDN workflow (Gcore). This is a separate
opt-in workflow, not part of the named platform profiles.

## Key variables

- `gcore_api_url` — Gcore API endpoint
- `gcore_api_key` — read from the `GCORE_API_KEY` environment variable

## Where applied

Invoked by `playbooks/edge-cdn.yml`. Not referenced by the canonical
`deploy_platform.yml` orchestrator.
