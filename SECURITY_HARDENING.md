# Security Hardening

## Enforced controls

- SSH host-key checking is enabled. Bootstrap connections use `accept-new`,
  which rejects changed keys; production operators should prepopulate known
  host keys when an out-of-band fingerprint is available.
- Generated platform credentials persist only in an Ansible-Vault-encrypted
  file. Missing encryption tooling/password material is fatal.
- Helm tasks that must interpolate credential values are censored with
  `no_log`; charts use Kubernetes Secret references wherever supported.
- Vault and External Secrets Operator use internal TLS and CA verification by
  default. Vault initialization material is encrypted locally and is not stored
  in a Kubernetes Secret or automated unseal CronJob.
- Argo CD defaults to TLS and explicit repository, namespace, and resource
  allowlists.
- GitLab chart 10 uses explicit external PostgreSQL, Dragonfly, and object
  storage. Passwords are Kubernetes Secrets, not Helm literal values.
- The protected Docker compatibility Runner never mounts a host Docker socket.
  Its exact digest-pinned DinD service is the only privileged job container,
  and it can schedule only on one label-selected, `NoSchedule`-tainted CI
  worker. Build/helper containers remain non-privileged with RuntimeDefault
  seccomp and dropped capabilities. The CI worker has no public IP, is not a
  load-balancer target, and is excluded from local-PV discovery.
- Elasticsearch uses the Basic license and verified TLS clients; no forged
  license artifact or paid-feature bypass is present.
- External CLI/manifests changed by this audit are version pinned and verified
  with SHA-256 before execution/application. CI setup actions are pinned to
  immutable commits.
- Coroot uses pinned official operator/CE charts and pinned application,
  agent, and ClickHouse images. Its eBPF privilege exception is confined to
  the dedicated `coroot` namespace.
- Node log collectors are confined to the `logging-agents` namespace, whose
  privileged Pod Security level permits required hostPath log access. The
  collectors are not privileged containers: host logs are read-only, state
  uses dedicated writable paths, and Docker-era directories and control
  sockets are removed from the rendered workloads.
- Backup, upgrade, rollback, health, and teardown paths exit nonzero when a
  required operation or verification fails.

## Secret bootstrap

```bash
umask 077
openssl rand -base64 48 > ~/.vault_pass
export ANSIBLE_VAULT_PASSWORD_FILE="$HOME/.vault_pass"
```

Protect that file through the workstation credential/backup policy. Do not
commit `.platform-secrets.yml`, Vault initialization output, kubeconfigs,
tokens, or restore artifacts.

## Network exposure

Control-plane and worker servers have private addresses. Hetzner firewalls are
replaced from complete declared policies, avoiding append-only stale rules.
GitLab, Argo CD, Grafana, Vault, and other administrative routes are intended
for the private/admin gateway. Review DNS and Gateway API resources after every
change.

The default bastion SSH source list is broad for bootstrap compatibility.
Public SSH is fail-closed. Set `network.ssh_source_ips`,
`hetzner_ssh_source_ips`, or comma-separated `HETZNER_SSH_SOURCE_IPS` to
operator/VPN CIDRs before provisioning. The infrastructure role rejects an
empty list, `0.0.0.0/0`, and `::/0`; HTTP/HTTPS and VPN transport ports retain
their independent public rules.

## Object-storage credential boundary

The platform deploys one in-cluster SeaweedFS S3 service. Public
`s3.<platform-domain>` and compatibility hostnames are routes to that same
service, not additional object stores. The SeaweedFS root identity remains
only in the S3 authentication configuration and is never copied into GitLab,
database, observability, backup, or CI runtime Secrets.

Runtime access is split into bounded identities:

- GitLab application buckets;
- PostgreSQL, MongoDB, Vault, and native backup buckets;
- Loki and Tempo buckets;
- GitLab Runner archive cache;
- protected and development Nx cache buckets; and
- an ephemeral bucket-bootstrap identity scoped to the declared bucket list.

SeaweedFS static IAM expresses these grants as `Action:bucket`, for example
`Write:gitlab-registry`. The role refuses duplicate root/runtime keys and
enables the storage namespace default-deny policy by default. New consumers
must receive a dedicated identity or join an explicitly documented bucket
group; do not copy `object_storage_access_key` into a workload.

Credential rotation is additive and ordered: persist the new scoped keys in
the encrypted platform secrets file, reconcile the SeaweedFS auth Secret and
wait for the S3 gateway rollout, update consumer Secrets/restart consumers,
prove allowed bucket access and a cross-bucket `AccessDenied`, and only then
remove the retired credential. Never rotate the S3 config and consumers in the
opposite order.

Nx cache retention is enforced in the single SeaweedFS service, not by
deploying another S3 implementation. The audited `4.25.1` chart applies
30-day protected and 7-day development path TTL before the cache workloads are
enabled. Because SeaweedFS 4.25 stamps that TTL on new writes, an existing
bucket must be drained before first policy activation. Native per-bucket quotas
bound logical cache growth; a digest-pinned five-minute CronJob changes an
over-quota bucket to read-only and a blocking initial Job closes the install
race. Do not set `CACHE_TTL_HOURS` or `CACHE_MAX_BYTES` on an S3-backed
`remotecache` 3.0.0 server: those controls apply only to its filesystem cache.
Changing the SeaweedFS chart version requires explicitly extending the audited
version gate after verifying both the TTL hook and quota shell commands.

## Supply-chain maintenance

When changing a pinned binary or manifest:

1. Use the upstream release/version, never a moving `latest` URL.
2. Obtain the digest from an upstream checksum/provenance source or calculate
   and independently verify it.
3. Update the version and checksum together.
4. Run the complete validation suite.
5. Review rendered Kubernetes/Helm changes before deployment.

## HIPAA option

Set `compliance.hipaa.enabled: true` in `platform.yaml`, or run
`./platform.sh enable hipaa`, to select the additional technical controls. The
deployment reconciles host audit rules, verifies Vault TLS and Cilium network
encryption, and installs active SSN/phone/email replacement pipelines in the
selected Promtail, Filebeat, or Fluentd collector. The legacy direct variable
`hipaa_compliance=true` remains compatible for direct Ansible users.

These pattern replacements are defense in depth, not proof that PHI cannot
reach logs. This option does not establish legal/organizational compliance,
sign a BAA, define retention policy, or replace application data controls,
risk assessment, access review, incident response, and an audit program. See
[HIPAA_COMPLIANCE.md](HIPAA_COMPLIANCE.md) for the exact automated boundary.

## Validation

```bash
bash scripts/validate-local.sh
python3 scripts/preflight_check.py --project-root "$PWD"
```

CI/local checks are static and parser-focused. Validate live network exposure,
RBAC, Pod Security, certificate chains, backups, restores, and audit delivery
against an authorized test environment before production rollout.
