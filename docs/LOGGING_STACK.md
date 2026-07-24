# Logging Stack Selection

The Kubernetes logging layer supports Loki, Elasticsearch with Filebeat, or
Elasticsearch with Fluentd. Bastion and control-plane host log shipping is
disabled by default because the platform does not expose an unauthenticated
NodePort ingestion endpoint.

## Default behavior

- minimal/small → Loki single-binary
- medium → ELK
- medium-optimized → Loki single-binary with seven-day retention
- production → ELK

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

## Node-agent security boundary

Promtail, Filebeat, and Fluentd run in the dedicated `logging-agents` namespace. Pod
Security Admission is `privileged` for that namespace because node-wide log
collection requires hostPath access, but the Filebeat container itself is not
privileged. It runs as UID 0 only to read host logs, mounts `/var/log` read-only
for containerd CRI logs, and has a separate writable hostPath for its registry
state. The pinned Elastic chart's obsolete `/var/lib/docker/containers` and
`/var/run/docker.sock` mounts are removed by a fail-closed Helm post-renderer;
Filebeat does not access the containerd control socket either. Promtail's
upstream Docker-directory default is likewise replaced with only
`/var/log/pods` and its `/run/promtail` registry state.
Fluentd also disables both upstream automatic host mounts, receives `/var/log`
read-only, and writes positions/buffers only to its dedicated
`/var/lib/fluentd-logging-agents-data` hostPath.
Filebeat and Fluentd explicitly tolerate both current and legacy Kubernetes
control-plane `NoSchedule` taints. Health checks compare each selected logging
DaemonSet's desired and ready counts with the complete registered node count,
so silently collecting logs from workers only is a deployment failure.

The namespace has default-deny ingress/egress plus explicit egress only to
cluster DNS, the Kubernetes API used for metadata discovery, and the selected
Loki or Elasticsearch service. Elasticsearch separately allows port 9200
ingress from `logging-agents`. Coroot's eBPF node agent remains the distinct
intentional privileged-container boundary in the dedicated `coroot` namespace.

ELK and EFK collectors authenticate as the dedicated
`platform_logging_ingest` principal. Its role is limited to cluster monitoring,
ILM/template setup, and management/ingest of `filebeat-*` and `fluentd-*`
indices. Fluentd uses create-only bulk writes to stay within that role. The
`elastic` superuser secret is never replicated; collector CA and
ingest secrets are removed from `logging-agents` when ELK/EFK is deselected.
