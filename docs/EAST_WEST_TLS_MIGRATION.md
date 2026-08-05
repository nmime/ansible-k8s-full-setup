# East-West TLS Migration

This runbook controls the migration from private-network-only protection to
verified application TLS for traffic inside `n0xeid-medium-optimized-cx`.
Private addressing, Cilium NetworkPolicy, credentials, and WireGuard remain in
place; TLS adds peer authentication and protects same-node traffic as well as
cross-node traffic.

## Safety invariants

1. Migrate clients before disabling a plaintext listener.
2. Verify DNS names against a private CA; never use an insecure or
   allow-invalid-certificate option as a cutover mechanism.
3. Keep the old listener during a dual-listener phase and remove its
   NetworkPolicy access only after live connection telemetry proves it unused.
4. Require a fresh backup and a healthy replica before restarting a stateful
   primary.
5. Do not rename an operator CR, Service, or PVC during a TLS migration.
6. A successful TLS handshake is not sufficient: run an authenticated protocol
   command and separately prove that the plaintext command fails after
   enforcement.

## Migration ledger

| Boundary                               | Client preparation                                                                                                        | Enforced state                                                                                                                               | Rollback                                                                                       |
| -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| PostgreSQL through PgBouncer           | Alias certificate and CA distributed; clients use `verify-full`                                                           | `spec.tlsOnly=true`; final `pg_hba` client rule is `hostssl`, followed by `host ... reject`                                                  | Restore `databases.postgresql.tls_mode: preferTLS`; keep the alias, CA, CR, Services, and PVCs |
| MongoDB                                | Completed 2026-08-03: combined datastore CA is mounted and Fun clients use the stable alias with `tls=true` and `tlsCAFile` | Percona CR is `requireTLS`, `allowInvalidCertificates=false`, and `certManagementPolicy=userProvidedOnly`; authenticated verified-TLS passed and plaintext failed | Restore `preferTLS`; do not replace the replica set or certificates                            |
| Dragonfly                              | Completed 2026-08-03: Fun, GitLab, Blackbox, Social Agents, and Steel use `rediss://` or verified TLS on `dragonfly-tls...:6380` | Remote `6379` is denied by Dragonfly ingress; the HAProxy-to-server leg is pod loopback only | Restore `dragonfly.allow_plaintext_clients: true` and the old `redis://dragonfly...:6379` URI while keeping the TLS listener |
| Gateway to HTTP backend                | Backend receives a private certificate and exposes an HTTPS port; route uses that port                                    | Attach a `BackendTLSPolicy` with the exact Service DNS identity                                                                              | Point the route back to the previous HTTP port and remove only that policy                     |
| Native HTTP/gRPC storage and telemetry | Add server TLS and CA mounts per protocol, with dual listeners where supported                                            | Deny the plaintext port after probes, agents, and scrapers are migrated                                                                      | Re-enable the previous port and policy; retain the CA and TLS listener                         |

## PostgreSQL proof

The cutover gate queries `pg_stat_activity` joined to `pg_stat_ssl` from the
current primary and refuses to enable `tlsOnly` while any remote client backend
is plaintext. After reconciliation, verify the effective rules:

```bash
primary_pod="$(kubectl get pods -n databases \
  -l postgres-operator.crunchydata.com/cluster=n0xeid-medium-optimized-cx-pg,postgres-operator.crunchydata.com/role=primary \
  -o jsonpath='{.items[0].metadata.name}')"

kubectl exec -n databases "$primary_pod" -c database -- \
  psql -Atqc "select line_number,type,auth_method from pg_hba_file_rules order by line_number"

kubectl exec -n databases "$primary_pod" -c database -- \
  psql -Atqc "select coalesce(version,'PLAINTEXT'),count(*) from pg_stat_activity a left join pg_stat_ssl s using(pid) where a.backend_type='client backend' and a.client_addr is not null group by version order by version"
```

Run a temporary least-privilege client with the CA mounted. Require
`sslmode=verify-full` to return `ssl=true` and TLS 1.2 or newer. Repeat with
`sslmode=disable` and require a connection failure. Do not print the URI or
password in the receipt.

## MongoDB client phase

Every Mongo-using container must mount the CA at
`/etc/mongodb/tls/ca.crt`. Effective URIs must meet all of these conditions:

- seed: `n0xeid-mongo.databases.svc.cluster.local:27017`;
- `replicaSet=rs0` and the existing `authSource` are retained;
- `tls=true`;
- `tlsCAFile=/etc/mongodb/tls/ca.crt`;
- no `tlsInsecure`, `tlsAllowInvalidCertificates`, or
  `tlsAllowInvalidHostnames` option.

Verify the effective Secret without decoding credentials:

```bash
for namespace in fun-games-preproduction fun-games-production; do
  kubectl get secret datastore-client-ca -n "$namespace" \
    -o jsonpath='{.data.ca\.crt}' | base64 -d | \
    awk '/BEGIN CERTIFICATE/{count++} END{print namespace, count " CA certificates"}' \
    namespace="$namespace"
done
```

The 2026-08-03 cutover rolled and proved pre-production before production.
Every API, game server, bot, scheduler, autobot, and migration hook received
the CA mount. Both Argo applications finished `Synced` and `Healthy`; public
readiness/liveness checks returned 200. The Percona CR is generation 15,
operator-observed, `ready 3/3`, and `requireTLS`. A password-authenticated
hostname-verifying canary passed, while the same plaintext command failed with
a closed server connection.

## Dragonfly cutover

The operator-managed Dragonfly process keeps port `6379` during client
migration. An HAProxy sidecar terminates TLS 1.2+ on port `6380` and forwards
only over `127.0.0.1:6379` in the same pod. Its certificate SAN includes the
full `dragonfly-tls.dragonfly.svc.cluster.local` name. The sidecar watches the
projected certificate and reloads after cert-manager rotation.

The platform publishes `datastore-client-ca` to declared consumer namespaces.
It contains only the PostgreSQL alias CA, PostgreSQL operator-cluster CA,
MongoDB CA, and Dragonfly CA certificates. Both PostgreSQL CAs are required:
pooled runtime traffic uses the stable PgBouncer alias, while owner migrations
connect directly to the operator primary. Private CA keys remain in their
owning namespaces.

Client URI:

```text
rediss://:<password>@dragonfly-tls.dragonfly.svc.cluster.local:6380/<db>
```

The Fun pre-production and production clients passed this phase on 2026-08-03.
GitLab then moved webservice, Sidekiq, KAS, toolbox, Workhorse, and exporter
clients to the chart's `rediss` configuration. Dragonfly was returned to
standalone mode so KAS/Rueidis cannot follow an emulated `CLUSTER SLOTS`
response to an advertised plaintext pod address. GitLab health passed, its
Redis keyspace remained readable, and the KAS network namespace showed only
connections to `6380`.

Blackbox monitoring mounts the datastore CA and uses a TLS TCP module with the
exact Dragonfly Service DNS name. Its live probe negotiated TLS 1.3 and returned
`probe_success 1`.

Social Agents and Steel were promoted in order: pre-production, the internal
`agents` release, then production. Each stage had healthy workloads before the
next stage started. The production Argo applications finished `Synced` and
`Healthy`, and the internal Helm releases finished `deployed`. The final master
socket snapshot after recovery contained `0` remote established connections on
`6379` and `406` on `6380`.

After that zero-client gate, `dragonfly.allow_plaintext_clients` was set to
`false` and the Dragonfly role reconciled successfully. An authenticated,
hostname-verifying TLS canary wrote, read, and deleted a temporary key. The
same client rejected a wrong server name, and a connection attempt to the old
plaintext Service port failed. The Dragonfly process retains its loopback
backend listener for HAProxy, while Dragonfly ingress exposes only `6380` to
application namespaces. Consumer-owned egress policies must explicitly allow
`6380` when a namespace already enforces egress isolation. The Dragonfly role
removes its former namespace-wide partial egress policy because selecting every
pod while allowing only Dragonfly blocked DNS and every unrelated dependency.
GitLab's existing Cilium egress allowlist now permits Dragonfly `6380` instead
of legacy `6379`; Fun uses Dragonfly ingress plus its own namespace policies.
Loopback `127.0.0.1:6379` sockets are the expected TLS-proxy backend legs and
are not plaintext remote clients.

## Gateway and native-service phases

Do not create a `BackendTLSPolicy` until the referenced Service exposes a real
TLS port with a certificate whose SAN matches the policy hostname. Migrate one
route at a time and require all Gateway API conditions to be `True`, then test
the public or VPN origin. A policy attached to an HTTP-only port causes an
outage; it does not add TLS to the backend.

For Loki, VictoriaMetrics, OpenTelemetry, ClickHouse, SeaweedFS, Coroot, and
other native protocols, use the component's supported server-TLS settings or a
sidecar only when native dual-listener migration is unavailable. Scrapers,
agents, backup jobs, and health probes are clients and must move before the
plaintext port is denied.

## Evidence and rollback receipt

For every boundary, retain:

- configuration revision and live resource generation;
- certificate subject, SANs, issuer, and expiry without private material;
- positive authenticated TLS result and negotiated version;
- expected hostname-validation or plaintext failure;
- workload readiness and error-rate observation;
- exact rollback field, Service port, and NetworkPolicy restored during a
  rehearsal.

Never claim an entire cluster is TLS-only while any ledger row is still in its
client-preparation or dual-listener phase.
