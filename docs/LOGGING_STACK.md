# Logging Stack Selection

The Kubernetes logging layer supports Loki, Elasticsearch with Filebeat, or
Elasticsearch with Fluentd. Bastion and control-plane host log shipping is
disabled by default because the platform does not expose an unauthenticated
NodePort ingestion endpoint.

## Default behavior

- minimal/small → Loki
- medium/production → ELK

## Override

```bash
ansible-playbook playbooks/deploy_platform.yml -e log_stack=efk -e tier=production ...
```

## Access

- **Loki**: Grafana Explore → Loki datasource
- **ELK/EFK**: Kibana at `https://kibana.{domain}` or Grafana → Elasticsearch datasource

To ship bastion logs, first provide a private authenticated endpoint with a
matching TLS certificate, then set `bastion_log_shipping=true`,
`bastion_log_ingest_host`, and `bastion_log_ingest_port`. Control-plane host
shipping is a separate opt-in (`control_plane_log_shipping=true`); Kubernetes
container collection is already handled by the in-cluster agent.
