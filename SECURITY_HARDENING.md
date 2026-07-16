# Security Hardening

## Enforced controls

- SSH host-key checking is enabled. Bootstrap connections use `accept-new`,
  which rejects changed keys; production operators should prepopulate known
  host keys when an out-of-band fingerprint is available.
- Generated platform credentials persist only in an Ansible-Vault-encrypted
  file. Missing encryption tooling/password material is fatal.
- Vault and External Secrets Operator use internal TLS and CA verification by
  default. Vault initialization material is encrypted locally and is not stored
  in a Kubernetes Secret or automated unseal CronJob.
- Argo CD defaults to TLS and explicit repository, namespace, and resource
  allowlists.
- GitLab chart 10 uses explicit external PostgreSQL, Dragonfly, and object
  storage. Passwords are Kubernetes Secrets, not Helm literal values.
- Elasticsearch uses the Basic license and verified TLS clients; no forged
  license artifact or paid-feature bypass is present.
- External CLI/manifests changed by this audit are version pinned and verified
  with SHA-256 before execution/application. CI setup actions are pinned to
  immutable commits.
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
Restrict `hetzner_ssh_source_ips` to operator/VPN CIDRs before production use.

## Supply-chain maintenance

When changing a pinned binary or manifest:

1. Use the upstream release/version, never a moving `latest` URL.
2. Obtain the digest from an upstream checksum/provenance source or calculate
   and independently verify it.
3. Update the version and checksum together.
4. Run the complete validation suite.
5. Review rendered Kubernetes/Helm changes before deployment.

## HIPAA option

`hipaa_compliance: true` enables additional technical controls such as internal
TLS assertions and log-redaction configuration. It does not establish legal or
organizational compliance, sign a BAA, define retention policy, or replace a
risk assessment and audit program.

## Validation

```bash
bash scripts/validate-local.sh
python3 scripts/preflight_check.py --project-root "$PWD"
```

CI/local checks are static and parser-focused. Validate live network exposure,
RBAC, Pod Security, certificate chains, backups, restores, and audit delivery
against an authorized test environment before production rollout.
