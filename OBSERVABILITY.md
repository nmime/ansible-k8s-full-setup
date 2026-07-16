# Observability Stack

The selectable observability core combines metrics, logs, dashboards, database
monitoring, and alerting. Tracing, Coroot, APM, and Blackbox are separate
dependants that can be added later.

## Components and pins

| Capability | Technology | Current pin |
|---|---|---|
| Metrics | VictoriaMetrics Operator with VMSingle or VMCluster, VMAgent, VMAlertmanager, VMAlert, and VMRules | operator chart `0.66.2` |
| Dashboards | Grafana | chart `10.5.15` |
| Database monitoring | Percona PMM Server | image `3.8.1` |
| Compact logging | Loki + Promtail | charts `6.55.0` + `6.17.1` |
| Medium/production logging | Elasticsearch/Kibana + Filebeat; Fluentd remains supported for `efk` custom profiles | Elastic `9.4.3`, Filebeat chart `8.5.1`, Fluentd chart `0.5.2` |
| Distributed tracing | Tempo + OpenTelemetry Collector | chart/image `1.6.1`/`2.6.1` and `0.102.1`/`0.112.0` |
| Application topology | Coroot CE/operator, node/cluster agents, ClickHouse | see below and [technology catalog](docs/TECHNOLOGY_CATALOG.md) |
| Synthetic probes | Prometheus Blackbox Exporter + VMProbe | chart `11.15.1` |
| Elastic tracing intake | Elastic APM Server | image `9.4.3` |

## Core selection

```yaml
observability:
  enabled: true
  metrics:
    enabled: true
  logging:
    enabled: true
    stack: elk       # loki, elk, or efk
  grafana:
    enabled: true
```

Metrics, logging, and Grafana are intentionally one tested bundle. Profile
validation rejects partial core selections. `minimal` and `small` use VMSingle
and Loki; `medium`, `medium-optimized`, and `production` use VMCluster and ELK.
`resource_tier`, not only capability `tier`, controls the storage/replica
defaults, so `medium-optimized` keeps VMCluster/ELK with compact sizing.

## Tracing

`tracing.enabled` requires observability plus object storage. Tempo stores data
in the SeaweedFS `tempo-traces` bucket and the OpenTelemetry Collector accepts
OTLP inputs. Retention defaults are 12 hours for minimal/small resource tiers,
24 hours for medium, and 72 hours for production.

```bash
./platform.sh enable tracing
./platform.sh deploy tracing
```

## Coroot

`coroot.enabled` requires observability. It is on by default for `medium`,
`medium-optimized`, and `production`, and off for smaller profiles. The role
uses the official operator flow with these pins:

- operator chart `0.9.7` (operator application `1.9.5`);
- CE chart `0.3.3`, Coroot application `1.23.3`;
- node agent `1.34.2`, cluster agent `1.7.1`;
- ClickHouse `25.11.2-ubi9-0`.

The flow follows Coroot's
[Kubernetes installation](https://docs.coroot.com/installation/kubernetes/)
and [operator configuration](https://docs.coroot.com/installation/k8s-operator/)
model; the deprecated standalone chart/OCI reference is not used.

Coroot reuses VictoriaMetrics for query and remote-write endpoints instead of
deploying a duplicate Prometheus. It keeps one ClickHouse shard/replica. The
`medium-optimized` profile caps application/agent requests and uses 10 GiB for
Coroot plus 20 GiB for ClickHouse. Its eBPF node agent requires privileged Pod
Security admission; the exception is scoped to the `coroot` namespace. The
operator and cluster agent are denied global Secret reads. The UI is exposed
only through the VPN/admin Gateway at `https://coroot.<domain>`.

```bash
./platform.sh enable coroot
./platform.sh deploy coroot
```

## Alerting and log redaction

VMAlertmanager routes exist only when their channel is selected. Telegram
needs `ALERT_TELEGRAM_BOT_TOKEN` and `ALERT_TELEGRAM_CHAT_ID`. Email needs
Postal plus an `alerting.email.to` destination.

When `compliance.hipaa.log_redaction_enabled` is active, the deployed
Promtail, Filebeat, or Fluentd configuration performs SSN/phone/email-shaped
replacement before shipping. This is defense in depth; applications must still
avoid logging sensitive data.

## Discovery and validation

The VictoriaMetrics path uses `VMServiceScrape` resources. Optional Prometheus
compatibility resources can be enabled where a role supports them. Health
tasks verify the core operator/storage/logging/dashboard services and tracing
components; Coroot installation separately waits for its application,
cluster-agent Deployment, and node-agent DaemonSet rollout.

```bash
kubectl get vmagent,vmsingle,vmcluster,vmalertmanager,vmalert -A
kubectl get pods -n monitoring
kubectl get pods -n elasticsearch
kubectl get pods,pvc -n coroot
helm list -A
./scripts/health-gates.sh
```
