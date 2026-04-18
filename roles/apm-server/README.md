# APM Server (Elastic APM / OTLP)

Distributed tracing backend. Reuses the existing Elasticsearch X-Pack Platinum cluster as trace storage — no new component storage required.

## What it does
- Runs Elastic APM Server (matched to ES version)
- Accepts **OTLP gRPC + HTTP** on `:8200`, as well as native Elastic APM agents and Jaeger/Zipkin protocols
- Writes traces → `apm-*` indices in existing Elasticsearch
- Applies ILM policy `apm-rollover-30-days` (rolls over at 30GB / 7d, deletes after 14d)
- Kibana APM UI is already present in ES 9.x — service maps, transactions, dependencies

## Dependencies
- `roles/elasticsearch` must run first (`elasticsearch-credentials` Secret + Service)
- `elastic` user has sufficient privileges (superuser)

## Wiring apps
Set these env vars in any service:
```
OTEL_EXPORTER_OTLP_ENDPOINT=http://apm-server.elasticsearch.svc.cluster.local:8200
OTEL_SERVICE_NAME=<your-service>
OTEL_TRACES_SAMPLER=parentbased_traceidratio
OTEL_TRACES_SAMPLER_ARG=0.1   # 10% sampling in prod
```
No token required (anonymous OTLP ingest is enabled, RUM origins wildcarded — safe because service is in-cluster only).

## Access
- Internal: `apm-server.elasticsearch.svc.cluster.local:8200`
- UI: Kibana APM UI (VPN-only via admin-gateway)

## Toggles
- `deploy_apm: true` in profile to enable
- Requires ES (`elasticsearch` role, always on)
