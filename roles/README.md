# Ansible Roles

`playbooks/deploy_platform.yml` is the canonical orchestrator. The exhaustive
user-facing selector and version matrix is in
[`docs/TECHNOLOGY_CATALOG.md`](../docs/TECHNOLOGY_CATALOG.md).

| Role | Managed scope |
|---|---|
| `generate-secrets` | strong credential generation and Ansible-Vault-encrypted persistence |
| `hetzner-infra` | Hetzner network, subnets, firewall, servers, placement, load balancer, volumes, DNS inputs |
| `network-security` | bastion packages/hardening, UFW, fail2ban, NAT, Headscale/Caddy VPN, node-exporter, auditd |
| `ha-egress` | cross-location standby gateway, protected Floating IPv4, explicit static SNAT, health checks, and provider route failover |
| `k8s-cluster-management` | Kubespray, Kubernetes/containerd, Cilium/Hubble/encryption, Gateway API, cert-manager, Hetzner webhook/CCM/CSI, MetalLB |
| `k8s-secrets` | pinned Vault Raft/TLS deployment and optional External Secrets Operator integration |
| `object-storage` | active SeaweedFS S3-compatible implementation |
| `elasticsearch` | Elasticsearch Basic and Kibana with TLS, security, and resource-tier sizing |
| `k8s-observability` | VictoriaMetrics, Grafana, Loki/Promtail or ELK/EFK collectors, PMM, alerting, optional Tempo/OTel and Coroot |
| `k8s-databases` | Percona PostgreSQL/PgBouncer/pgBackRest and optional Percona MongoDB/PBM |
| `dragonfly` | Dragonfly operator and Redis-compatible cache |
| `gitlab-selfhosted` | GitLab CE, Gitaly, Registry, KAS, Toolbox, and optional Runner |
| `k8s-gitops` | Argo CD with constrained projects/sources/resources |
| `k8s-autoscaling` | KEDA event-driven autoscaling |
| `temporal` | Temporal server, UI, and admin tools |
| `postal` | Postal plus MariaDB, using Dragonfly for queues |
| `apm-server` | Elastic APM/OTLP ingestion into Elasticsearch |
| `blackbox-exporter` | Prometheus Blackbox Exporter and VictoriaMetrics probes |
| `glitchtip` | Sentry-compatible error tracking using PostgreSQL and Dragonfly |
| `daytona-deployment` | optional Daytona workspace platform |
| `backup-restore` | scheduled backup/verification resources and restore-drill support |
| `hipaa-hardening` | assertions/reporting for the optional host-audit, encryption, and active redaction control set |
| `edge-cdn` | separate explicit multi-region edge/Gcore workflow; not part of named profiles |

`seaweedfs-storage` is not referenced by the canonical playbook and must not be
treated as a second active object-storage implementation.

Current secrets pins are Vault `2.0.3`, HashiCorp Vault chart `0.34.0`, and
External Secrets Operator chart `2.7.0`. The complete pinned-version inventory
is maintained in the technology catalog and enforced by the version matrix.

## Canonical order and gates

The main playbook imports roles in this order, while `when` expressions skip
unselected services:

1. generate secrets;
2. Hetzner infrastructure;
3. network security;
4. Kubernetes and cluster add-ons;
5. secrets and object storage;
6. Elasticsearch and the observability bundle;
7. databases, Dragonfly, GitLab, Argo CD, and KEDA;
8. Temporal, Postal, APM, Blackbox, GlitchTip, and Daytona;
9. backup automation and HIPAA-oriented hardening post-tasks.

Profile normalization is tagged `always`, so targeted role/tag execution uses
the same dependency contract as `deploy all`. Do not bypass a disabled flag
with an extra variable: use `platform.sh enable COMPONENT`, validate, and then
deploy it.

Vault initialization material is encrypted locally. The role does not create
a Kubernetes Secret or CronJob containing root/unseal material and does not
claim Kubernetes-based auto-unseal.

## Capability versus resource tier

All active roles distinguish:

- `tier`: minimal, small, medium, or production capability set;
- `resource_tier`: requests, limits, retention, storage, and default stateless
  replica envelope.

`medium-optimized` therefore keeps `tier: medium` and
`resource_tier: small`. Stateful quorum services retain safe topology while
recoverable stateless workloads use compact defaults and explicit caps. Coroot
also reuses VictoriaMetrics and uses a one-replica ClickHouse layout in this
profile instead of adding a second Prometheus deployment.
