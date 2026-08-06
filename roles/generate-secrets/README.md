# generate-secrets

Generates all platform credentials once and persists them to a local
Ansible-Vault-encrypted file. On subsequent runs existing credentials are
loaded, never regenerated.

## Key variables

- `secrets_file` — path to the encrypted secrets file

## Where applied

First role executed by `playbooks/deploy_platform.yml` before any component
that needs credentials. See `docs/SECURITY_HARDENING.md` for migration details.
