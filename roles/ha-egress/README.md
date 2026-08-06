# ha-egress

Active/passive cross-location private-network egress with a movable public
identity (Floating IPv4). Provides explicit static SNAT, health checks, and
provider route failover.

## Key variables

- `ha_egress_enabled` — disabled unless a profile selects `network.egress.enabled`

## Where applied

Invoked by `playbooks/deploy_ha_egress.yml`. Both the standby server and
Floating IP are billable Hetzner resources, so this is opt-in only.
