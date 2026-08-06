# hetzner-infra

Provisions Hetzner Cloud infrastructure: networks, subnets, firewalls,
bastion, control-plane and worker servers, load balancers, volumes, and DNS
inputs.

## Key variables

Server-type overrides and topology variables are set in `defaults/main.yml`
and overridden per profile. See `docs/HETZNER_CAPACITY_TARIFFS.md` for the
full tariff matrix.

## Where applied

First infrastructure role in `playbooks/deploy_platform.yml`.
