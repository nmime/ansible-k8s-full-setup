# Static high-availability egress

The platform can route every private Kubernetes node through one of two
active/passive egress gateways while preserving one public source address.
The public identity is a protected Hetzner Floating IPv4; Primary IPs remain
per-server SSH and recovery addresses and are never treated as movable.

## Topology and failure model

The existing bastion is gateway 1. `network.egress.standby_*` declares gateway
2 in another `eu-central` location with a fixed private address. Both gateways
carry the Floating IPv4 as a local `/32`, have IP forwarding enabled, and own
an explicit `SNAT --to-source <floating-ip>` rule. Hetzner delivers the
Floating IPv4 only to its assigned server, and the Network has exactly one
`0.0.0.0/0` route pointing at the active gateway.

Each gateway exposes private `/readyz`, `/healthz`, and `/metrics` endpoints.
The monitoring namespace grants only VictoriaMetrics egress to both private
metrics endpoints; the gateway port is not exposed to the public internet.
The standby watchdog reads provider state and promotes itself only after the
configured number of consecutive active-health failures. Promotion assigns
the Floating IPv4 first, replaces the provider Network route, waits for the
new health check, and rolls both changes back if verification fails. There is
no automatic failback: use a verified manual promotion to avoid oscillation.
Existing TCP sessions can reset during failover; SMTP and normal application
clients must retry.

## Safe deployment

1. Set `network.egress.enabled: true` and `activate: false`.
2. Run `playbooks/deploy_ha_egress.yml` with the live profile and a write-capable
   `HCLOUD_TOKEN`. This creates the billable standby and Floating IPv4 but
   leaves the existing MASQUERADE path authoritative.
3. Verify the reported Floating IPv4 and set `manage_mail_dns: true`,
   `mail_legacy_ipv4` to the old mail IP, `mail_include_legacy_ipv4: true`, and
   `activate: true`. Re-run the playbook. It publishes HELO A/PTR/SPF, waits for
   two public resolvers, activates static SNAT, and enables both watchdogs.
4. Set Postal `outbound_ipv4` to the Floating IPv4 and reconcile Postal.
5. After DNS TTL and outbound mail verification, set
   `mail_include_legacy_ipv4: false` and reconcile again.

## Operations

On either gateway:

```text
sudo platform-egressctl status
sudo platform-egressctl promote <provider-server-name>
systemctl status platform-egress-watchdog.timer
journalctl -u platform-egress-watchdog.service
```

`promote` refuses a target whose private readiness check or provider server
state is unhealthy. The controller token is stored root-only on both gateways;
it is never placed in tracked configuration or command output. Hetzner API
tokens are project-scoped, so the project containing these gateways must not
also contain unrelated infrastructure with a different trust boundary.

## Drill acceptance

A release is complete only when all of the following have evidence:

- a private node reports the Floating IPv4 at an independent address service;
- provider state agrees on Floating-IP owner and default-route gateway;
- manual promotion and failback both restore the same public source IPv4;
- VictoriaMetrics scrapes two ready gateways and exactly one active gateway;
- port 25, HELO A/PTR, SPF, DKIM, DMARC, and both Postal sender domains pass;
- a real message from each sender domain is accepted by an external mailbox.
