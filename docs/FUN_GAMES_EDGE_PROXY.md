# Fun Games Russian-reachable edge

This playbook replaces the legacy SafeLine installation only after a
side-by-side canary and a complete hostname gate. The canonical services remain
on `n0xeid.xyz`; compatibility entrypoints retain their public
`funfiesta.games` authorities.

The S3 contract is intentionally non-redirecting:

- public compatibility endpoint: `https://s3.funfiesta.games`
- canonical upstream and TLS SNI: `s3.n0xeid.xyz`
- incoming `Host: s3.funfiesta.games` is preserved for AWS SigV4
- request/response buffering is disabled for multipart and streaming traffic
- the direct cluster route remains available for DNS-only rollback

## Inventory

Store credentials in Ansible Vault, not in Git:

```yaml
fun_games_edge:
  hosts:
    ru-edge-1:
      ansible_host: 212.193.26.64
      ansible_user: root
```

Define each hostname and its certificate paths on the edge host:

```yaml
fun_games_edge_hosts:
  - hostname: s3.funfiesta.games
    upstream_host: s3.n0xeid.xyz
    upstream_sni: s3.n0xeid.xyz
    health_path: /
    health_statuses: [403]
    certificate_path: /data/safeline/resources/nginx/certs/cert_4.crt
    certificate_key_path: /data/safeline/resources/nginx/certs/cert_4.key
```

Before a production cutover, the inventory must cover every hostname in
`fun_games_edge_required_hosts`. This prevents stopping SafeLine while an
existing game/API entrypoint still depends on it. The fail-closed list includes
all production and `*.pp.funfiesta.games` UNO/Durak frontend, API, backend,
bot and admin names, plus S3.

Each entry may define an exact `health_path` and accepted `health_statuses`.
The canary and production gates test both the local proxy liveness endpoint and
a real request through the configured upstream; a healthy Nginx process with a
dead origin cannot pass.

The outer edge discards any client-supplied forwarding chain, rejects unknown
authorities, enforces container CPU/memory/PID limits, and proxies HTTP-01
requests to the origin so certificate renewal remains possible.

## Gates

Audit makes no changes:

```bash
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=audit
```

Canary binds only loopback ports `18080/18443`, leaves SafeLine running, and
performs TLS health checks:

```bash
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=canary
```

Run authenticated list, multipart upload/download, Range, and delete tests
against the canary before cutover. Then configure every required hostname and
invoke the explicit gate:

```bash
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=cutover \
  -e fun_games_edge_confirm_cutover=true
```

The controller environment must provide `CLUSTER_BACKUP_AGE_RECIPIENT` and the
`BACKUP_DR_*` endpoint, region, bucket and credentials. The edge host must have
`age` and the AWS CLI. Before SafeLine is stopped, the playbook archives its
configuration and certificates, encrypts the archive, uploads the ciphertext
and checksum to DR storage, downloads the ciphertext, and requires an exact
round-trip checksum. Plaintext and verification copies are removed in an
`always` block.

Only after that gate does the playbook start the pinned and hardened Nginx
proxy, verify every hostname and real upstream response, and restore SafeLine
automatically if any verification fails. Change GCore DNS only after this
playbook succeeds. Explicit rollback is:

```bash
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=rollback
```
