# Vault Secret Governance and Recovery Boundary

This document defines the credential source of truth, Kubernetes delivery
model, operator access path, rotation order, and disaster-recovery boundary.
It intentionally contains no credential values.

## Source-of-truth matrix

| Material | Authoritative location | Kubernetes form | Recovery copy |
|---|---|---|---|
| Long-lived application and platform credentials | Vault KV v2, exact environment-scoped path | ExternalSecret-managed Secret or workload-side Vault integration | Encrypted, offline recovery bundle where a provider cannot reissue it |
| Generated platform bootstrap credentials | Vault KV v2 at `clusters/<project>/platform/generated` after one-time seed | Purpose-specific controller/chart Secret | Ansible-Vault-encrypted seed, stored outside the cluster |
| Database passwords and cloud/provider credentials | Prefer dynamic Vault credentials; otherwise exact KV v2 path | Short-lived projected/materialized credential | Provider recovery procedure; offline escrow only when reissue is impossible |
| cert-manager TLS keys and ACME account state | cert-manager controller | Controller-owned TLS/Opaque Secret | Encrypted cluster backup; do not create a second writer in Vault |
| Kubernetes ServiceAccount tokens | Kubernetes TokenRequest API | Audience-bound projected token | None; mint again after recovery |
| Helm release records | Helm/Kubernetes | `helm.sh/release.v1` Secret | Git plus cluster backup; never copy to Vault KV |
| Vault unseal/recovery shares and emergency root procedure | Split out-of-band custody | Never a Kubernetes Secret | Independent encrypted media held by separate custodians |
| Ansible Vault password, backup age identity, rebuild SSH/provider access, external S3 restore credentials | Independent operator escrow | Never only inside the protected cluster or Vault | At least two tested, geographically or administratively independent copies |

“All secrets in Vault” therefore means all long-lived operational credentials
for which Vault can safely be the source of truth. Putting Vault recovery keys,
the only backup credentials, or the only rebuild access inside the same Vault
creates a recovery loop and is prohibited.

## ESO boundary

The shared `vault-backend` store is a transition mechanism. It is constrained
by all of the following:

- exact KV-v2 paths; glob and parent traversal are rejected;
- an explicit namespace allowlist on the ClusterSecretStore;
- a dedicated ServiceAccount with automatic token mounting disabled;
- a dedicated token audience;
- 15-minute batch tokens with an explicit maximum TTL and no `default` policy;
- `read` only; no `list`, create, update, delete, or sudo capability;
- readiness verification after every reconciliation.

For stronger workload isolation, split the transition store into one
SecretStore, Vault policy, Kubernetes auth role, and ServiceAccount per trust
domain. A compromise in `analytics`, for example, must not authorize reads from
`production` application paths even if both namespaces use ESO.

## Generated credential mirror

The generate-secrets role creates a structured in-memory bundle from the same
values written to the encrypted `.platform-secrets.yml` file. Secrets
reconciliation handles the Vault mirror as follows:

1. Validate one exact `clusters/<project>/platform/generated` path.
2. Read it without logging data.
3. If absent, stage the bundle in mode-0600 transient files, write it once, and
   remove both files on every exit path.
4. If present, compare the complete dictionaries without logging them.
5. Fail on any drift. The only automatic exception is the atomic empty-to-set
   bootstrap of both Telegram alert fields. An external DR key rotation also
   requires an explicit one-run authorization and can change only the DR key
   pair; both paths use Vault KV-v2 compare-and-set.

Rotation is an explicit transaction: update the credential issuer and all
consumers, verify the new credential, revoke the old credential, update the
encrypted recovery seed when required, and only then accept the new Vault
version. Reset `vault_platform_generated_dr_rotation_allowed` to its default
`false` immediately after the authorized reconcile.

Changing only the Telegram destination uses the same fail-closed model. Set
`VAULT_PLATFORM_GENERATED_ALERT_DESTINATION_ROTATION_ALLOWED=true` for one
secrets reconcile after updating the encrypted recovery seed. The reconcile
requires the bot token and every unrelated credential to remain byte-for-byte
identical, permits only `alert_telegram_chat_id` to change, and writes the new
Vault KV-v2 version with compare-and-set. The authorization is absent again on
the next process invocation.

## Human and emergency Vault access

The initial non-expiring root token is a bootstrap credential, not an
administrator login. Before revoking it:

1. Configure a human identity method backed by the organization identity
   provider and a least-privilege administrator policy.
2. Replace reconciliation that depends on the initial root token with a
   narrowly scoped, short-lived automation identity. It must not be able to
   edit its own policy or auth role.
3. Update restore automation to generate a temporary root token from the
   approved recovery/unseal quorum, perform the recovery, and revoke the token.
4. Run an isolated snapshot restore drill and a human-access drill.
5. Revoke the initial root token and verify that routine reconciliation and
   backup jobs still work.

Do not revoke the initial root token while restore and reconciliation still
depend on the stored token. That creates an outage loop rather than improving
security.

## Backup boundary and restore order

Vault Raft snapshots must be copied to storage independent of the Kubernetes
cluster, its primary object store, its cloud account where practical, and the
Vault being backed up. The restore credential and its decryption identity must
be available before Vault or Kubernetes exists.

Restore in this order:

1. Recover operator SSH/provider access, the encrypted repository/state bundle,
   and its decryption identity from out-of-band custody.
2. Rebuild the Kubernetes control plane and restore etcd/PKI/config state as the
   selected recovery mode requires.
3. Restore and unseal Vault from the independently stored Raft snapshot.
4. Verify a non-sensitive sentinel and Vault audit logging.
5. Reconcile the restricted auth roles and ESO stores.
6. Let ESO materialize application credentials and then start consumers.
7. Rotate any credential exposed during the incident or recovery session.

A successful upload is not restore evidence. Schedule isolated restore drills,
record the exact snapshot identifier and result, and alert when evidence ages
beyond the approved recovery-point objective.

## Kubernetes encryption migration

New Kubespray inventory uses `secretbox`, an authenticated encryption provider.
Changing the provider configuration alone does not rewrite existing Secret
records. For an existing cluster:

1. Back up etcd and verify recovery material.
2. Add `secretbox` as the first provider while retaining the prior provider for
   reads.
3. Roll API servers one at a time and verify quorum and API health.
4. Rewrite every Secret through the API without printing `.data` or
   `.stringData`.
5. Prove every stored Secret is encrypted with the new provider.
6. Remove the legacy provider and plaintext `identity` fallback, then roll and
   verify again.

Do not perform this migration as part of unrelated secret rotation or while
backup/restore evidence is missing.

## Safe operator access

Never print, decode, or pipe entire Secret objects through diagnostic templates.
Inspect metadata only with JSON processors that construct a new object before
output. Do not use a Go template expression that may fail while holding Secret
objects; some clients include the complete input object in template error
messages.

Use the protected controller kubeconfig rather than copying credentials into a
shell history:

```bash
export KUBECONFIG="$PWD/.campaign-state/<project>/controller/home/.kube/config"
kubectl cluster-info
kubectl get nodes
```

Vault operator commands should receive tokens through protected files, stdin,
or short-lived login responses. Never pass a token as a command-line argument,
environment assignment recorded by shell history, or chat message.

## Rotation after possible disclosure

Treat every long-lived value returned by a Secret API during an unsafe
diagnostic session as compromised. Rotate by dependency family, not all at
once:

1. external backup/storage credentials and prove a fresh snapshot;
2. provider, Git, registry, deploy, and runner tokens;
3. database users and service-to-service passwords using overlapping validity
   where supported;
4. application JWT/signing material using dual-key verification before old-key
   removal;
5. human/admin passwords and notification integrations;
6. revoke old versions and verify audit logs contain no subsequent use.

Each family needs a rollback window and a consumer check. Never delete the old
credential before the issuer, Vault value, materialized Secret, rollout, and
canary have all been verified.
