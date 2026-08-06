# dragonfly

Deploys the Dragonfly operator and a Redis-compatible Dragonfly cache instance.

## Key variables

- `dragonfly_namespace` — target namespace (default: `dragonfly`)
- `dragonfly_operator_version` — operator release pin
- `dragonfly_image_version` — Dragonfly image pin

## Where applied

Included by `playbooks/deploy_platform.yml` when the `dragonfly` component is
selected by the active profile. Used by GitLab, Postal, and GlitchTip as their
cache layer.
