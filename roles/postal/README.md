# postal

Deploys Postal SMTP server with MariaDB, using Dragonfly for message queues.

## Key variables

- `postal_namespace` — default `postal`
- `postal_version` — image tag pin
- `postal_web_hostname` — management UI hostname

## Where applied

Included by `playbooks/deploy_platform.yml` when `postal` is explicitly opted
in. Postal is a shared cluster-wide SMTP platform service.
