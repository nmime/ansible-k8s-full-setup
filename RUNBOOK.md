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

## Bounded live load and evidence

Plan the exact profile-aware load first. Dry-run writes the evidence schema and
phase plan but does not require a reachable cluster or mutate Kubernetes:

```bash
./scripts/tier-load-test.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig /absolute/path/to/tier.kubeconfig \
  --output /secure/evidence/tier-name \
  --run-id tier-name-01 \
  --dry-run
```

For a disposable live cluster, set `ANSIBLE_VAULT_PASSWORD_FILE` so the Vault
phase can read the encrypted `.campaign-state/PROJECT/.vault-init-PROJECT.json`, then remove
`--dry-run`. The defaults increase concurrency and operation counts from
`minimal` through `production`; explicit bounds are available when a smaller
step is needed:

```bash
./scripts/tier-load-test.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig /absolute/path/to/tier.kubeconfig \
  --vault-init .campaign-state/PROJECT/.vault-init-PROJECT.json \
  --clients 8 \
  --http-requests 5000 \
  --s3-objects 250 \
  --pg-transactions 2500 \
  --vault-operations 500 \
  --dragonfly-requests 10000 \
  --phase-timeout 900 \
  --max-error-percent 1 \
  --max-restart-delta 10
```

The phases use pinned OCI clients and execute sequentially inside one cluster.
After every phase, temporary S3 objects, PostgreSQL tables, Vault metadata, and
Dragonfly keys are removed before a new health snapshot. A failed cleanup,
timeout, node-pressure condition, unavailable controller, unbound PVC,
unhealthy certificate/route, APIService failure, or excessive restart delta is
a hard stop. Interrupt traps attempt the same cleanup; if the API was lost,
inspect the failed phase log and verify its `tier-load/RUN_ID` prefix manually.

The output contract is:

- `summary.json`: run/profile, bounds, pinned images, overall result, phases;
- `phases.tsv`: operations, errors, error percentage, duration, log path;
- `logs/`: one log per enabled load phase;
- `evidence/STAGE/evidence.json`: health and readiness counts;
- `resources.tsv`, `top-nodes.tsv`, `top-pods.tsv`, `warning-events.tsv`.

Evidence collection is read-only and never retrieves Secrets. It can also be
run independently:

```bash
./scripts/collect-live-evidence.sh \
  --config platform-orchestrator/platform.yaml \
  --kubeconfig /absolute/path/to/tier.kubeconfig \
  --output /secure/evidence/operator-check \
  --stage operator-check
```

For concurrent tier campaigns, give every invocation a distinct kubeconfig,
run ID, and output directory. Never point parallel processes at a shared active
context.

The repository's five-profile controller does this automatically. When the
default Hetzner `cx` pool returns `resource_unavailable`, rerun with
`./run_all.sh --capacity-family cpx ...`; the mapping preserves each profile's
CPU/RAM floor and topology. Do not compensate for provider capacity by lowering
node counts or selecting a type that the role's capacity assertions reject.
After evidence capture, parallel teardown is safe because resource selection
uses the exact `project` label; always verify that no campaign-labeled resources
or campaign DNS records remain.

If all five Kubespray recaps are successful but a later role fails, resume with
`--skip-kubespray` and a fresh isolated campaign root. Keep the same projects,
domains, API ports, capacity overrides, DR endpoint, and immutable source
commit. The Hetzner CCM must become ready and remove the external-cloud-provider
taints before the DNS smoke pod is scheduled; DNS verification intentionally
runs after that gate.

`run_all.sh` defaults to `--controller-forks 1`: all five profiles remain live
in parallel, but each controller advances one host operation at a time. Keep
that bound on memory-constrained workstations and raise it only after checking
RAM and swap headroom.

Five-tier controllers keep encrypted Vault initialization material in
`.campaign-state/PROJECT/`, outside disposable worktrees. Preserve that
gitignored directory and the matching vault password in the operator backup;
never initialize again merely because a resume controller cannot find it.

For Postal, verify both schema reconciliation and the unprivileged SMTP
listener before treating the mail stack as healthy:

```bash
kubectl get job postal-schema-reconcile -n postal
kubectl logs job/postal-schema-reconcile -n postal
kubectl logs deployment/postal-smtp -n postal | grep 'Listening on :::2525'
kubectl get service postal-smtp -n postal -o yaml
```

The Service exposes 25/587 and targets 2525. Gateway API traffic is admitted
to default-deny service namespaces through the Cilium `ingress` identity; a
plain namespace selector for the host-network Envoy DaemonSet is insufficient.

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
and enabled Kubernetes resources. Extra servers and server type changes always
fail closed; the infrastructure role cannot bulk-delete or bulk-resize cluster
members. Use the migration controller, which checkpoints every operation and
drains, validates, and restores one node at a time:

```bash
./scripts/migrate-profile.sh --target medium plan
./scripts/migrate-profile.sh --target medium execute
./scripts/migrate-profile.sh status
./scripts/migrate-profile.sh finalize
```

After any SeaweedFS topology change, verify both the configured placement and
the live replica count. The role performs these checks automatically; the
operator-level evidence commands are:

```bash
kubectl exec -n storage seaweedfs-master-0 -- sh -c \
  "printf 'volume.list\n' | weed shell -master=127.0.0.1:9333"
kubectl exec -n storage seaweedfs-master-0 -- sh -c \
  "printf 'volume.fix.replication -collectionPattern=* -doDelete=false -verbose\n' | weed shell -master=127.0.0.1:9333"
```

For `medium`, `medium-optimized`, and `production`, every listed volume must
show `ReplicaPlacement:001` and the dry-run output must contain no
`under replicated` line.

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
kubectl get backupstoragelocation,backup -n velero

# Review every isolated restore path before a recovery exercise.
./scripts/restore-drill.sh --component postgresql --backup PGBACKREST_SET --dry-run
./scripts/restore-drill.sh --component mongodb --backup BACKUP_CR --dry-run
./scripts/restore-drill.sh --component vault --backup VAULT_SNAPSHOT --dry-run
./scripts/restore-drill.sh --component seaweedfs --backup VELERO_BACKUP --dry-run
./scripts/restore-drill.sh --component gitlab --backup TOOLBOX_BACKUP_ID --dry-run

export CLUSTER_BACKUP_AGE_RECIPIENT=age1...
./platform-orchestrator/platform.sh backup-cluster \
  --recipient "$CLUSTER_BACKUP_AGE_RECIPIENT" --force
./platform-orchestrator/platform.sh restore-cluster \
  --archive /secure/k8s-cluster-....tar.gz.age --mode verify
```

GitLab recovery needs the Toolbox archive and the separately stored Rails
encryption secret; its external PostgreSQL data comes from the independently
gated pgBackRest set, not the Toolbox archive. Vault recovery needs the original
snapshot token and enough unseal shares to reach its threshold. Production
recovery also needs external Velero/Kopia data and the encrypted
etcd/PKI/config bundle. Backup object existence is not restore proof; run all
five isolated component drills and replacement-cluster drills on a schedule. See
[BACKUP_RESTORE.md](BACKUP_RESTORE.md).

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

An upgrade reconciles software inside the current profile. It cannot change
profile or topology. Profile migration supports every distinct source/target
pair across the five named profiles. For example, to move to production:

Configure these values once in the gitignored, mode-`0600` `.env`; operational
scripts load them automatically:

```dotenv
BACKUP_DR_ENDPOINT=https://s3.example-provider.com
BACKUP_DR_BUCKET=company-platform-dr
BACKUP_DR_ACCESS_KEY=...
BACKUP_DR_SECRET_KEY=...
CLUSTER_BACKUP_AGE_RECIPIENT=age1...
```

```bash
./platform-orchestrator/platform.sh migrate --target production plan
./platform-orchestrator/platform.sh migrate execute \
  --target production \
  --dr-endpoint "$BACKUP_DR_ENDPOINT" --dr-bucket "$BACKUP_DR_BUCKET" \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"
./platform-orchestrator/platform.sh migrate status
./platform-orchestrator/platform.sh migrate finalize \
  --backup-recipient "$CLUSTER_BACKUP_AGE_RECIPIENT"
```

The durable workflow backs up first, expands to the maximum source/target node
counts, drains/resizes each retained node separately, grows both the provider
disk and the node root filesystem, verifies etcd before and after control-plane
changes, applies target capabilities, migrates
VictoriaMetrics single-to-cluster or cluster-to-single, validates, and backs
copies post-switch VictoriaMetrics samples back, restores the Helm/config
baseline, and deliberately retains expanded nodes. Finalization is itself
checkpointed: it removes disabled dependants first, retires old metrics/logging
PVCs, removes excess workers then control planes through Kubespray, reconciles
the exact target, takes a final backup, removes disabled backup resources last,
and cleans unused cloud placement resources.

Every completed node resize is followed by the full profile-aware platform
health gate before the next node is touched. This includes expected node and
database replica counts, workload readiness, storage, certificates, routes,
security controls, and Helm release state. Vault is unsealed after each node
restart before that gate runs.

Provider root disks are grow-only. If two Hetzner server types have identical
CPU and memory but the current type has a larger root disk, the migration
retains that type and records the explicit override in
`node-type-retention.tsv` and the active config. A transition that would need
both a disk shrink and a compute-shape change fails before the backup/mutation
stages and requires a separately planned one-node replacement workflow.

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
project-prefixed Hetzner compute/network resources plus all CSI volumes captured
by attachment to the project's servers or, for a context whose nodes all match
the project, by the PV's Hetzner CSI volume handle. It preserves DNS and the
global kubeconfig. The orchestrator passes the configured
`k8s_api_local_port`, so teardown stops only that project's matching tunnel and
removes only its project-specific known-hosts file. Review the backup inventory
before authorizing teardown.
