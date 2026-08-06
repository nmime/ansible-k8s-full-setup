# Documentation Index

Every document in this directory with a one-line purpose.

## Getting started

| Document | Purpose |
|---|---|
| [DEPLOYMENT.md](DEPLOYMENT.md) | Step-by-step platform provisioning and profile selection guide. |
| [ACCESS_AND_CREDENTIALS.md](ACCESS_AND_CREDENTIALS.md) | How node, cluster, and service credentials are generated and accessed. |
| [TECHNOLOGY_CATALOG.md](TECHNOLOGY_CATALOG.md) | Exhaustive selector table, version matrix, and per-component description. |
| [RESOURCE_OWNERSHIP_MAP.md](RESOURCE_OWNERSHIP_MAP.md) | Which component owns which Kubernetes namespace, chart, or resource. |

## Operations

| Document | Purpose |
|---|---|
| [RUNBOOK.md](RUNBOOK.md) | Day-2 operations runbook: health checks, common fixes, escalation. |
| [BACKUP_RESTORE.md](BACKUP_RESTORE.md) | Ordered backup verification and disaster-recovery commands. |
| [UPGRADE_RUNBOOK.md](UPGRADE_RUNBOOK.md) | Staged upgrade workflow with preflight, canary, rollback, and health gates. |
| [TEST_DR_ENDPOINT.md](TEST_DR_ENDPOINT.md) | Durability and restore-endpoint testing procedure. |

## Networking and edge

| Document | Purpose |
|---|---|
| [DNS_AND_TRAFFIC_FLOW.md](DNS_AND_TRAFFIC_FLOW.md) | DNS hierarchy, Gateway API topology, and public/private traffic flow. |
| [HA_EGRESS.md](HA_EGRESS.md) | Cross-location standby egress gateway with Floating IP failover. |
| [CDN_ROUTING_MODES.md](CDN_ROUTING_MODES.md) | Edge CDN routing mode reference. |
| [CONFIGURE_CDN_FOR_FRONTEND.md](CONFIGURE_CDN_FOR_FRONTEND.md) | How to configure the CDN for a frontend application. |
| [EDGE_CDN_USAGE_GUIDE.md](EDGE_CDN_USAGE_GUIDE.md) | Operational guide for the multi-region edge/Gcore CDN workflow. |
| [FUN_GAMES_EDGE_PROXY.md](FUN_GAMES_EDGE_PROXY.md) | Fun-Games edge proxy design and configuration. |

## Security and compliance

| Document | Purpose |
|---|---|
| [SECURITY_HARDENING.md](SECURITY_HARDENING.md) | Platform-wide technical security hardening controls. |
| [SECURITY_OVERVIEW.md](SECURITY_OVERVIEW.md) | High-level security posture summary. |
| [SECURITY_AUDIT.md](SECURITY_AUDIT.md) | Security audit findings and remediation status. |
| [HIPAA_COMPLIANCE.md](HIPAA_COMPLIANCE.md) | HIPAA-oriented hardening scope and mapping. |
| [VAULT_SECRET_GOVERNANCE.md](VAULT_SECRET_GOVERNANCE.md) | Vault deployment, External Secrets Operator integration, and secret lifecycle. |
| [EAST_WEST_TLS_MIGRATION.md](EAST_WEST_TLS_MIGRATION.md) | Plan for migrating pod-to-pod traffic to mutual TLS. |

## Observability

| Document | Purpose |
|---|---|
| [OBSERVABILITY.md](OBSERVABILITY.md) | Metrics, logging, tracing, and alerting stack overview. |
| [LOGGING_STACK.md](LOGGING_STACK.md) | Loki/ELK logging pipeline configuration and retention. |
| [LOGGING_SECURITY_AUDIT.md](LOGGING_SECURITY_AUDIT.md) | Log redaction, access control, and audit findings. |

## Cost and capacity

| Document | Purpose |
|---|---|
| [COST_MODEL.md](COST_MODEL.md) | Per-profile monthly cost arithmetic and assumptions. |
| [HETZNER_CAPACITY_TARIFFS.md](HETZNER_CAPACITY_TARIFFS.md) | Live CX/CAX/CPX/CCX tariff matrix and capacity monitoring. |

## CI and automation

| Document | Purpose |
|---|---|
| [CI_AUTOMATION.md](CI_AUTOMATION.md) | CI pipeline structure, gates, and local validation parity. |
| [GITLAB_CI_CLASSIFICATION.md](GITLAB_CI_CLASSIFICATION.md) | CI environment classification and runner assignment rules. |
| [GITLAB_RUNNER_BOOTSTRAP.md](GITLAB_RUNNER_BOOTSTRAP.md) | First-cluster GitLab Runner token bootstrap procedure. |

## Upgrade plans

| Document | Purpose |
|---|---|
| [GITLAB_UPGRADE_PLAN.md](GITLAB_UPGRADE_PLAN.md) | GitLab chart version upgrade path and risk assessment. |
| [PG_OPERATOR_UPGRADE_PLAN.md](PG_OPERATOR_UPGRADE_PLAN.md) | Percona PostgreSQL operator upgrade plan. |
| [VAULT_UPGRADE_PLAN.md](VAULT_UPGRADE_PLAN.md) | Vault chart/version upgrade plan. |

## Historical reports

| Document | Purpose |
|---|---|
| [FIVE_TIER_LIVE_TEST_2026-07-21.md](FIVE_TIER_LIVE_TEST_2026-07-21.md) | Five-profile live deployment test report (2026-07-21). |

## Meta

| Document | Purpose |
|---|---|
| [KNOWN-DEBT.md](KNOWN-DEBT.md) | Intentionally unfixed items with risk justification. |
