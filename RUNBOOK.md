# Platform Operations Runbook

## Operating principles

- Stop on failed preflight, backup, migration, Helm, readiness, or health gate.
- Never mutate production during a restore drill.
- Record the platform config, exact Helm revisions, backup IDs, and command
  output for every maintenance window.
- Do not rotate Vault unseal material or GitLab Rails secrets casually; losing
  either can make persisted data unrecoverable.
- The repository deploys platform services, not an application workload.

## Routine checks

```bash
./platform-orchestrator/platform.sh status
./scripts/health-gates.sh
kubectl get nodes
kubectl get pods -A
helm list -A
kubectl get certificate -A
kubectl get cronjob -A
```

Investigate any non-Ready node, failed Helm release, missing required component,
expired certificate, or missed backup before continuing maintenance.

## Deployment and reconciliation

```bash
cd platform-orchestrator
./platform.sh init medium
$EDITOR platform.yaml
./platform.sh deploy all
```

For the full medium service set on the constrained resource envelope, initialize
`medium-optimized`. Confirm the generated config still contains
`platform_profile: medium-optimized`, `tier: medium`, and
`resource_tier: small` before deployment.

Reruns reconcile firewall rules, load-balancer services/targets, DNS records,
and enabled Kubernetes resources. Extra servers and server type changes fail
closed. Drain affected nodes first, then explicitly opt into destructive
reconciliation only during an approved window:

```bash
ansible-playbook playbooks/deploy_platform.yml \
  -e @platform-orchestrator/platform.yaml \
  -e hetzner_allow_destructive_reconcile=true
```

## Component lifecycle

```bash
cd platform-orchestrator
./platform.sh components
./platform.sh enable COMPONENT
./platform.sh validate
./platform.sh deploy COMPONENT
```

Enabling also enables required foundations and validates the resulting config.
Targeted deploys run the same normalization contract as a full deployment.
This is the normal way to add a technology after the initial cluster build.
The accepted component names are `object-storage`, `secrets`, `eso`,
`databases`, `postgresql`, `mongodb`, `elasticsearch`, `dragonfly`, `gitlab`,
`gitlab-runner`, `gitops`, `observability`, `coroot`, `tracing`, `autoscaling`,
`temporal`, `postal`, `backup`, `glitchtip`, `apm`, `blackbox`, `daytona`, and
`hipaa`. See the [technology catalog](docs/TECHNOLOGY_CATALOG.md) for the exact
dependency and profile matrix.

Disabling only changes desired selection; it does not stop or delete already
installed workloads. That boundary is deliberate, so a temporary pause remains
easy to reverse:

```bash
./platform.sh disable COMPONENT
# Later:
./platform.sh enable COMPONENT
./platform.sh deploy COMPONENT
```

If capacity must be reclaimed, verify backups, disable dependants first, then
remove the disabled component. The component name must be repeated exactly.
PVC-backed or otherwise data-bearing services require the extra destructive
flag:

```bash
./platform.sh remove blackbox --confirm blackbox
./platform.sh remove databases --confirm databases --delete-data
```

Removal is scoped to Kubernetes component resources. Hetzner infrastructure,
DNS, remote backup objects, and the tracing bucket are retained. A later enable
after `--delete-data` is a fresh deployment until data is restored.

Coroot is data-bearing because its namespace contains application and
ClickHouse PVCs. Back up or export required history, then use `--delete-data`
for removal. The eBPF node agent uses the privileged admission level only in
the `coroot` namespace; investigate any privilege expansion outside it.

`remove hipaa` is intentionally rejected. Disabling HIPAA-oriented hardening
stops future reconciliation, but audit rules and other security controls must
be reversed individually under an approved policy/change record.

## Secrets and Vault

Secret generation requires `ANSIBLE_VAULT_PASSWORD_FILE` or
`vault_password_file` to point to a protected regular file. The encrypted
`.platform-secrets.yml` is the persistence source for generated credentials.
There is no automatic plaintext path.

Vault uses internal cert-manager TLS. Initialization output is encrypted into
the configured local init file; Kubernetes does not receive a root-token or
unseal-key Secret/CronJob. Follow the organization’s approved manual or KMS
unseal procedure after pod/node recovery.

Checks:

```bash
kubectl exec -n vault vault-0 -- vault status
kubectl get certificate,secret -n vault
kubectl get clustersecretstore
```

## Backups

```bash
./scripts/backup-all.sh --dry-run
./scripts/backup-all.sh --force
kubectl get jobs -A -l app.kubernetes.io/part-of=backup-restore
```

GitLab recovery needs the Toolbox archive and the separately stored Rails
encryption secret. Backup object existence is not restore proof; run isolated
restore drills on a schedule. See [BACKUP_RESTORE.md](BACKUP_RESTORE.md).

## Upgrades

```bash
python3 scripts/preflight_check.py --project-root "$PWD"
./scripts/upgrade-platform.sh plan
./scripts/upgrade-platform.sh snapshot
./scripts/upgrade-platform.sh execute --component COMPONENT
```

The snapshot is a configuration baseline. It does not replace database,
repository, Vault, or object-storage backups. GitLab upgrades follow one minor
at a time and the required stops documented in
[docs/GITLAB_UPGRADE_PLAN.md](docs/GITLAB_UPGRADE_PLAN.md).

After each step:

```bash
./scripts/health-gates.sh
helm status RELEASE -n NAMESPACE
kubectl get jobs,pods -n NAMESPACE
```

## Rollback

```bash
./scripts/rollback.sh --component COMPONENT --snapshot snapshot/upgrade-TIMESTAMP
```

Rollback reads the recorded `helm-revisions.tsv` and restores the exact
revision/config baseline. If migrations or new writes crossed an incompatible
data boundary, stop writes and perform a same-version data restore instead of
treating Helm rollback as sufficient.

## Incident triage

### Cluster unreachable

1. Verify the configured kubeconfig and SSH tunnel.
2. Check Hetzner server/network/firewall state.
3. Reach the bastion and first control-plane private IP.
4. Inspect kubelet and control-plane service logs.
5. Do not run `heal` or delete pods until the control-plane cause is known.

### GitLab unavailable

1. Check external PostgreSQL, Dragonfly, and SeaweedFS first.
2. Inspect GitLab migrations, Toolbox, Webservice, Sidekiq, Gitaly, Registry,
   and KAS pods.
3. Verify `global.psql`, `global.redis`, object-storage secrets, and network
   policies in the current Helm values.
4. Run `scripts/gitlab-upgrade-check.sh` during upgrade incidents.

### Backup failed

1. Inspect the failed Job and all container logs.
2. Verify S3 endpoint and credentials from the affected namespace.
3. Confirm the expected CronJob exists and that concurrency policy did not
   suppress the run.
4. Trigger one manual job with `backup-all.sh --component NAME --force`.
5. Do not delete the last known-good artifact during investigation.

### Vault sealed or unavailable

1. Check TLS certificate/CA secrets and Vault pod/PVC state.
2. Run `vault status`; distinguish sealed from uninitialized.
3. Never reinitialize an existing Raft data set.
4. Use the approved recovery keys or restore a verified Raft snapshot into an
   isolated recovery procedure.

## Teardown

```bash
./platform-orchestrator/platform.sh destroy
```

The confirmation must match the project. The script verifies deletion of
project-prefixed Hetzner compute/network resources and preserves DNS and the
global kubeconfig. Review the backup inventory before authorizing teardown.
