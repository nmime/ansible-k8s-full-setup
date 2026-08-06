# k8s-secrets

Deploys and reconciles HashiCorp Vault in Raft mode with TLS, and optionally
integrates External Secrets Operator for Kubernetes-native secret syncing.

## Where applied

Included by `playbooks/deploy_platform.yml` for `small` and larger profiles.
See `docs/VAULT_SECRET_GOVERNANCE.md`.
