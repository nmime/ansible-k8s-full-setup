# network-security

Hardens bastion and worker nodes: UFW firewall rules, fail2ban, NAT, auditd,
Headscale/Caddy VPN, and node-exporter.

## Where applied

Executed by `playbooks/deploy_platform.yml` after infrastructure provisioning
and before or alongside cluster bootstrap.
