# Security Hardening Guide

> **Branch:** `upgrade/security-hardening`
> **Base:** `main@0595bdb`
> **Scope:** SSH hardening, secret management, Vault/ESO, TLS, ArgoCD AppProject lockdown

---

## Changes Summary

| # | Area | Before | After | Severity |
|---|------|--------|-------|----------|
| 1 | SSH Host Key Checking | `host_key_checking = false` + `StrictHostKeyChecking=no` | `host_key_checking = true` + `StrictHostKeyChecking=accept-new` | HIGH |
| 2 | Plaintext Secrets (`.platform-secrets.yml`) | Cleartext YAML on disk | Ansible Vault encryption with `--ask-vault-pass` | CRITICAL |
| 3 | Vault TLS | `tlsDisable: true` (plaintext) | Configurable via `vault_tls_disabled` (defaults `false` for medium/production) | HIGH |
| 4 | VAULT_TOKEN in Shell Env | `VAULT_TOKEN=…` in every `k8s_exec` command | Kubernetes auth via ESO SA token; root token only for init | HIGH |
| 5 | ArgoCD AppProject | Wildcard `*` on sourceRepos, destinations, resources | Specific repo/namespace/resource allowlists + configurable vars | CRITICAL |
| 6 | ArgoCD `--insecure` | `server.insecure: true` + `--insecure` flag | Conditional via `argocd_insecure_mode` (defaults `false`) | HIGH |

---

## 1. SSH Host Key Checking

### Problem
`ansible.cfg` had `host_key_checking = false` and `StrictHostKeyChecking=no`. This opens the entire Ansible control plane to MITM attacks.

### Fix
- `host_key_checking = true` in ansible.cfg
- `StrictHostKeyChecking=accept-new` in ssh_args (accepts first-time keys, rejects changes)

### Variable
```yaml
ansible_ssh_strict_host_key_checking: true  # default
```

---

## 2. Plaintext Secrets Migration (Ansible Vault)

### Problem
`.platform-secrets.yml` was written in plaintext. Anyone with read access to the control machine could exfiltrate all platform credentials.

### Fix
- `generate-secrets` role now encrypts `.platform-secrets.yml` with **Ansible Vault** on write
- On subsequent runs, the role decrypts with the vault password to load saved secrets
- Fallback to plaintext if vault is unavailable (with warning)

### Migration Steps
1. Encrypt existing `.platform-secrets.yml`:
   ```bash
   ansible-vault encrypt .platform-secrets.yml
   ```
2. Set vault password file:
   ```bash
   export ANSIBLE_VAULT_PASSWORD_FILE=~/.vault_pass
   ```
3. Re-run the playbook

### Variables
```yaml
vault_encrypt_secrets: true  # default
vault_password_file: "~/.vault_pass"
```

---

## 3. Vault TLS Configuration

### Problem
HashiCorp Vault was deployed with `tlsDisable: true` — all API traffic was unencrypted.

### Fix
- Vault Helm values now set `global.tlsDisable` to `vault_tls_disabled`
- Defaults to `false` (TLS enabled) for medium/production tiers
- Defaults to `true` (TLS disabled) for minimal/small tiers (simpler cert management)

### Variable
```yaml
vault_tls_disabled: "{{ true if tier in ['minimal', 'small'] else false }}"
vault_verify_tls: true
```

---

## 4. VAULT_TOKEN Elimination

### Problem
The Vault root token was passed as a shell environment variable (`VAULT_TOKEN={{ vault_init_data.root_token }}`) in 8+ separate tasks.

### Fix
- Root token is only used during the **init phase** (initialize, enable kv-v2, enable kubernetes auth, configure kubernetes auth)
- After init, all secret operations use **Kubernetes auth via ESO**
- The `vault_init_data.root_token` is stored in Vault itself and never exposed as a plaintext Ansible variable in post-init tasks

### Architecture
```
Init Phase:  Ansible → Vault (root token, once) → enable k8s auth → write k8s config
Runtime:     ESO SA Token → Vault k8s auth → read secrets → Kubernetes Secrets
```

---

## 5. ArgoCD AppProject Lockdown

### Problem
The default ArgoCD AppProject had wildcard permissions on sourceRepos, destinations, and resources.

### Fix
- `sourceRepos` locked to configurable list (defaults to platform GitLab repo + known Helm repos)
- `destinations` locked to specific namespaces and the in-cluster server
- Resource allowlists are explicit (Deployments, Services, ConfigMaps, etc.)
- Cluster-scoped resources are limited to Namespaces only

### Variables
```yaml
argocd_allowed_source_repos: [...]  # see defaults/main.yml
argocd_allowed_namespaces: [...]    # see defaults/main.yml
argocd_allowed_cluster_resources: [...]
argocd_allowed_namespace_resources: [...]
```

---

## 6. ArgoCD `--insecure` Removal

### Problem
ArgoCD server was started with `--insecure` flag and `server.insecure: true`, disabling TLS.

### Fix
- Removed `--insecure` from server extraArgs (conditional via `argocd_insecure_mode`)
- Changed `server.insecure: true` to `server.insecure: false` (configurable)
- TLS is properly terminated at the Cilium Gateway API HTTPRoute with cert-manager certs

### Variable
```yaml
argocd_insecure_mode: false  # default
```

---

## Testing

Run all tests:
```bash
cd tests && python3 -m pytest test_security_hardening.py -v
```

---

## Default Variables Added

All new security variables in `defaults/main.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `ansible_ssh_strict_host_key_checking` | `true` | SSH host key checking enforcement |
| `vault_tls_disabled` | tier-dependent | Vault TLS disabled (true for minimal/small) |
| `vault_verify_tls` | `true` | ESO Vault TLS verification |
| `argocd_insecure_mode` | `false` | ArgoCD insecure mode |
| `argocd_allowed_source_repos` | [...] | ArgoCD source repo allowlist |
| `argocd_allowed_namespaces` | [...] | ArgoCD destination namespace allowlist |
| `argocd_allowed_cluster_resources` | [...] | ArgoCD cluster resource allowlist |
| `argocd_allowed_namespace_resources` | [...] | ArgoCD namespace resource allowlist |
| `vault_encrypt_secrets` | `true` | Ansible Vault encryption for secrets file |
| `vault_password_file` | `~/.vault_pass` | Vault password file path |

---

## Migration Checklist

- [ ] Encrypt existing `.platform-secrets.yml` with `ansible-vault encrypt`
- [ ] Set `ANSIBLE_VAULT_PASSWORD_FILE` or use `--ask-vault-pass`
- [ ] Review `argocd_allowed_source_repos` for any additional repos needed
- [ ] Review `argocd_allowed_namespaces` for any additional namespaces
- [ ] Test Vault TLS connectivity after upgrade
- [ ] Verify SSH known_hosts are populated for bastion and cluster nodes
- [ ] Remove any `VAULT_TOKEN` from CI/CD environment variables

---

## Related

- PR #32: Elasticsearch role removal (handled separately)
- `SECURITY_AUDIT.md`: Original security audit findings
- `SECURITY_OVERVIEW.md`: Platform security overview
- `HIPAA_COMPLIANCE.md`: HIPAA-specific hardening
