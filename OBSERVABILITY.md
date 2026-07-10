# Observability Stack

Metrics (VictoriaMetrics), logs (Loki/ELK), traces (Tempo + OTel), alerting (VMAlertmanager), visualization (Grafana + PMM).

## Components

| Component | Chart | Image/Version |
|-----------|-------|---------------|
| VM Operator | `vm/victoria-metrics-operator` | `0.59.3` |
| Grafana | `grafana/grafana` | `10.5.15` |
| Loki | `grafana/loki` | `6.55.0` |
| PMM Server | `percona/pmm-server` | `2.47.0` |
| **Tempo** | `grafana/tempo` `1.6.1` | `2.6.1` |
| **OTel Collector** | `open-telemetry/opentelemetry-collector` `0.102.1` | `0.112.0` |

## Distributed Tracing

Gated by `tracing_enabled` (default: true for medium/production).

- **Protocols**: OTLP (gRPC/HTTP), Jaeger, Zipkin, OpenCensus
- **Storage**: S3 (SeaweedFS bucket `tempo-traces`)
- **Retention**: 12h (min/small), 24h (medium), 72h (production)
- **Metrics Generator**: writes trace metrics to VictoriaMetrics

## Service Discovery

All services scraped via `VMServiceScrape` CRDs (not Prometheus `ServiceMonitor`).

### Prometheus Compatibility
Set `prometheus_enabled=true` to also create `ServiceMonitor` CRDs.

## Alerting

| Group | Alerts |
|-------|--------|
| node | NotReady, MemoryPressure, DiskPressure, DiskUsageHigh |
| workload | CrashLoopBackOff, OOMKilled, UnavailableReplicas |
| databases | PostgresDown, ES-Red/Yellow, RedisDown |
| probes | ProbeFailed, SSLCertExpiring, HttpSlow |
| observability | VMAgentScrapeDrop, LokiIngestErrors |

Routing: Telegram (critical), Email/Postal (warning+), Grafana (default).

## Health Checks

`health_checks.yml` verifies: VM Operator, VMSingle/VMCluster, Loki, Grafana, Tempo, OTel Collector.

## Tier Matrix

| | minimal | small | medium | production |
|---|---------|-------|--------|------------|
| Metrics | VMSingle | VMSingle | VMCluster | VMCluster |
| Logs | Loki | Loki | ELK | ELK |
| Tracing | OFF | OFF | Tempo+OTel | Tempo+OTel |

## File Structure

```
roles/k8s-observability/
  tasks/
    main.yml             # orchestration
    alerting.yml         # VMAlertmanager + VMAlert + VMRules
    tracing.yml          # Tempo + OTel Collector
    health_checks.yml    # component health verification
  templates/
    vmservicescrapes.yml # VMServiceScrape CRDs
```
