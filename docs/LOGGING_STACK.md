# Logging Stack Selection

Supports Loki (lightweight), ELK (Elasticsearch+Filebeat+Kibana), or EFK (Elasticsearch+Fluentd+Kibana).

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

See full documentation: `docs/LOGGING_STACK.md`
