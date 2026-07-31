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

Define the independently renewed edge certificates as an exact, non-duplicated
partition of `fun_games_edge_hosts`, and provide the account email through
`FUN_GAMES_EDGE_ACME_EMAIL`:

```yaml
fun_games_edge_acme_certificates:
  - name: uno-production
    domains:
      - uno.funfiesta.games
      - api.uno.funfiesta.games
      - backend.uno.funfiesta.games
      - bot.uno.funfiesta.games
      - admin.uno.funfiesta.games
  - name: s3-production
    domains:
      - s3.funfiesta.games
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

Stage bootstrap certificates directly from ready Kubernetes TLS Secrets. The
staging playbook keeps the key material out of controller files and checks the
remaining lifetime, every SAN, and the certificate/private-key public-key
match before writing root-only files on the edge:

```bash
K8S_AUTH_KUBECONFIG=/absolute/path/to/kubeconfig \
ansible-playbook -i inventory.yml \
  playbooks/fun-games-edge-stage-certificates.yml
```

`fun_games_edge_bootstrap_certificates` maps each source namespace/Secret and
its exact domain set to the `certificate_path` and `certificate_key_path`
consumed by `fun_games_edge_hosts`. No TLS key belongs in Git or a local export
directory.

The outer edge discards any client-supplied forwarding chain, rejects unknown
authorities, and enforces container CPU/memory/PID limits. TLS keys are
group-readable only by the unprivileged Nginx container. Local Certbot HTTP-01
tokens are served by the edge; a missing local token is proxied to the origin,
so the origin cert-manager certificate remains independently renewable.

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
playbook succeeds.

After every public hostname resolves to the edge, enroll its independent
certificates and prove a complete staging renewal. This enables the host's
`certbot.timer`; successful future renewals pass through a strict certificate
allowlist, atomically replace the group-readable files, validate Nginx, and
reload it:

```bash
FUN_GAMES_EDGE_ACME_EMAIL=ops@example.com \
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=certificates
```

Do not leave the deployment between DNS cutover and this successful renewal
gate. The copied SafeLine/cert-manager certificates are bootstrap material,
not the long-term renewal mechanism. Explicit rollback is:

```bash
ansible-playbook -i inventory.yml playbooks/fun-games-edge.yml \
  -e fun_games_edge_mode=rollback
```
