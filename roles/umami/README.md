# umami

Deploys Umami privacy-focused web analytics. Image is pinned to the immutable
multi-architecture manifest for the upstream release.

## Where applied

Included by `playbooks/deploy_platform.yml` when `umami` is explicitly opted
in. Requires PostgreSQL and Dragonfly.
