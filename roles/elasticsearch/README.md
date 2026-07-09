# Elasticsearch Role

Deploys Elasticsearch and Kibana on Kubernetes with TLS, RBAC, and network policies.

## License

This role uses the **Elastic Basic license** only. The Basic license is self-generated
and included free with Elasticsearch OSS. It provides:

- Core search and indexing
- Security (TLS, authentication, authorization)
- Monitoring and cluster health APIs
- Snapshot and restore

Features **not** included in Basic (requires paid commercial licenses):
- Machine Learning
- Graph exploration
- Data stream rollups
- Watcher/alerting (beyond basic)
- SQL (beyond basic)
- Canvas/AI Ops

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `es_namespace` | `elasticsearch` | Kubernetes namespace |
| `es_version` | `9.4.1` | Elasticsearch version |
| `es_license_type` | `basic` | License type (Basic only) |
| `es_master_replicas` | Tier-dependent | Number of master nodes |
| `es_data_replicas` | Tier-dependent | Number of data nodes |
| `kibana_replicas` | `1` | Number of Kibana replicas |

## Resources Created

- Namespace (`elasticsearch`)
- TLS certificates (generated on first run)
- Secrets: `es-tls-certs`, `es-credentials`
- Services: `es-master-headless`, `es-data-headless`, `elasticsearch`
- StatefulSets: `es-master`, `es-data`
- Deployment: `kibana`
- HTTPRoute: `kibana` (admin-gateway)
- PDBs: `es-master-pdb`, `es-data-pdb`
- NetworkPolicy: `es-allow-internal`
- CiliumNetworkPolicy: `allow-filebeat-to-es`
- ServiceMonitor: `elasticsearch`

## Security

- TLS for HTTP and transport layers
- Run as non-root (UID 1000)
- Pod disruption budgets for high availability
- Network policies restrict ingress to internal services only
- Credentials stored in Kubernetes secrets with `no_log: true`
