# Daytona Deployment

Optional, disabled-by-default deployment of Daytona using the official Helm chart.

## Defaults

- Helm repository: `https://charts.daytona.io`
- Chart ref: `daytonaio/daytona`
- Chart version: `0.0.23`
- Namespace: `daytona`
- Base domain: `daytona.<domain>`

## Enable

```bash
ansible-playbook playbooks/deploy_platform.yml \
  --tags daytona \
  -e deploy_daytona=true \
  -e domain=example.com \
  -e email=admin@example.com
```

## Overrides

Use `daytona_values_override` to pass chart-specific values without editing the role:

```yaml
daytona_values_override:
  postgresql:
    enabled: true
  redis:
    enabled: true
```

The role renders the required chart keys (`baseDomain`, `services.*`, and dependency toggles) and recursively merges `daytona_values_override` on top.
