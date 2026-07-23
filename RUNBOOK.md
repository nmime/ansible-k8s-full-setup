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

### Controller API tunnel recovery

The managed controller tunnel keeps a stable loopback listener in front of the
reconnecting SSH forward. New Kubernetes and Helm connections are held for at
most 60 seconds while the supervisor rotates between control-plane endpoints.
Only bytes sent before the upstream TLS response are replayed; after the API
server responds, the connection uses normal fail-fast transport semantics.
The proxy never manufactures or caches a Kubernetes response. If no API server
becomes reachable within the bound, the operation fails closed and Ansible
stops normally.

The supervisor process owns both the retry proxy and SSH child. Its TERM trap
reaps both and removes the private Unix socket. An unexpected proxy exit is a
hard supervisor failure. This applies centrally to every client using the
generated kubeconfig, including Helm, `kubernetes.core` modules, and `kubectl`;
component-specific retries must not be used to hide chart or validation errors.

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
Restart growth is checked both as a cluster total and by stable
namespace/pod/container identity. A pod recreated with the same name and a new
UID counts as one replacement event. Pods created under new names during a
rollout or autoscaling are treated as new-name scale events, not inferred
container restarts; their actual `restartCount` still contributes to the
cluster-total gate while they remain present.

`live-tier-smoke.sh` validates every selected public Gateway route with normal
CA verification, TLS 1.2 or newer, and the route's declared path; it never uses
an insecure TLS bypass. The load harness also keeps certificate verification
enabled when `--http-url` selects HTTPS. For every profile whose configuration
enables the provider load balancer, each evidence snapshot must read the exact
Hetzner load balancer, match its HTTP/HTTPS destination ports to the live
Gateway Service, and observe healthy target checks. Provider reads are retried
for transient failures and then fail closed. The load-balancer-free `minimal`
profile records that this provider edge is not required.

The output contract is:

- `summary.json`: run/profile, bounds, pinned images, overall result, phases;
- `phases.tsv`: operations, errors, error percentage, duration, log path;
- `logs/`: one log per enabled load phase;
- `evidence/STAGE/evidence.json`: health and readiness counts;
- `resources.tsv`, `top-nodes.tsv`, `top-pods.tsv`, `warning-events.tsv`.

Evidence collection is read-only and never retrieves Secrets. Warning-event
messages are redacted for common token, password, secret, and access-key forms
before they are written. Terminal retry pods owned by a Job are evaluated from
the Job condition rather than counted as permanently unready workloads, while
a terminally failed Job remains fatal. Evidence can also be collected
independently:

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

The repository's five-profile controller does this automatically and defaults
to the current `cpx` balanced tariff. Use
`./run_all.sh --capacity-family cx|cax|cpx|ccx ...` to produce an explicit
economy x86, planning-only ARM64, balanced x86, or dedicated x86 mapping. Every
mapping preserves the profile's CPU/RAM floor and topology; live CAX deployment
is rejected until the full platform has an ARM64 production attestation. Do not
compensate for provider capacity by lowering node counts or selecting a type
that the role's capacity assertions reject.
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

Five-profile controllers keep encrypted Vault initialization material in
`.campaign-state/PROJECT/`, alongside the encrypted `.platform-secrets.yml`
credential source and outside disposable worktrees. Preserve that gitignored
directory and the matching vault password in the operator backup; never
initialize again or regenerate credentials merely because a resume controller
cannot find it.

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

That profile uses a hybrid storage policy. Run the capacity estimator against
the generated config and confirm the expected 470 GiB local reservation plus
280 GiB provider CSI capacity:

```bash
scripts/profile-storage-capacity.py \
  --source platform.yaml \
  --target platform.yaml
```
For a new cluster, the deploy playbook creates and verifies the static local
PV pool before stateful workloads. For an existing cluster, do not edit PVCs
or StatefulSet claim templates in place: take a complete external backup,
provision a replacement cluster, and use the verified `cluster-restore.sh`
plus `native-restore.sh` workflow. `migrate-profile.sh ... plan` prints the
immutable StorageClass transition map and `execute` fails closed.

Static local PV sizes are logical scheduler reservations, not hard directory
quotas. Keep `NodeDiskUsageHigh` and `NodeDiskPressure` alerts active, respond
before root usage reaches 85%, and never clean a retained local-PV directory
until the affected application member has been rebuilt and external recovery
has been verified.

Reruns reconcile firewall rules, load-balancer services/targets, DNS records,
and enabled Kubernetes resources. Extra servers and server type changes always
fail closed; the infrastructure role cannot bulk-delete or bulk-resize cluster
members. Use the migration controller, which checkpoints every operation and
drains, validates, and restores one node at a time:

Before `execute`, export the remote DR endpoint/bucket credentials and an age
recipient or pass the equivalent flags; set a volume quota large enough for
the source and target PVC inventories plus migration staging. `plan` prints the
exact required inputs and confirmation phrase. Do not rely on an undeclared
controller `.env` when writing an operator procedure.

```bash
./scripts/migrate-profile.sh --target medium plan
./scripts/migrate-profile.sh --target medium execute
./scripts/migrate-profile.sh status
./scripts/migrate-profile.sh finalize
```

For clusters deployed with `run_tier.sh --operator-state-root`, pass the same
root to the first migration command. The controller persists the exact secrets
and encrypted Vault-init paths for every resume, rollback, backup gate, and
finalize reconcile:

```bash
./scripts/migrate-profile.sh --target medium execute \
  --operator-state-root /state/cluster-a \
  --ssh-key-path /home/operator/.ssh/id_ed25519 \
  --ssh-known-hosts /state/controller-home/.ssh/known_hosts-cluster-a \
  --api-port 16444 \
  --volume-quota-gib 1500 --volume-safety-margin-gib 100
```

Without that option, migration retains the ordinary single-checkout defaults
in `playbooks/`. `--secrets-file` and `--vault-init-file` support layouts where
the two files are not under one operator-state root.

Multi-controller runs must retain their isolated `HOME` and per-project SSH
host-key database. If the private key is outside that `HOME`, pass its absolute
path with `--ssh-key-path` and pass the existing project host-key file with
`--ssh-known-hosts`. Both paths are persisted with the migration and explicit
resume/finalize values must match, preventing a reused IP from being trusted
through another cluster's shared `known_hosts` file.
Pass the already assigned controller tunnel port with `--api-port` for any
cluster not using `16443`. Migration persists it, embeds it in every generated
config, passes it to reconciliation, and rejects resume/finalize drift.

Use the exact account volume quota displayed by Hetzner; its API has no quota
field, so live migration refuses to infer one. The offline plan records
billable source/target claims, target growth, and backup scratch in
`volume-capacity-plan.json`. Live preflight combines that estimate with the
authoritative account-wide `hcloud volume list` total and a default 100 GiB
reserve. Resume compares provider volume IDs/sizes with the recorded baseline,
credits only growth mapped to this cluster's PVs, and fails on quota, margin, or
plan drift.

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
`gitlab-runner`, `gitops`, `observability`, `pmm`, `coroot`, `tracing`,
`autoscaling`, `temporal`, `postal`, `backup`, `glitchtip`, `apm`, `blackbox`,
`disaster-recovery`, `daytona`, and `hipaa`. See the
[technology catalog](docs/TECHNOLOGY_CATALOG.md) for the exact dependency and
profile matrix.

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
unseal procedure after ordinary pod/node recovery. Full replacement recovery
is the narrow exception: `native-restore.sh` may automate unseal only from that
separately protected Ansible-Vault-encrypted init file, streams shares over
stdin, verifies the internal CA, and removes its temporary token Secret.

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
export CLUSTER_BACKUP_AGE_IDENTITY=/secure/path/to/age-identity.txt
./platform-orchestrator/platform.sh backup-cluster \
  --vault-init-file playbooks/.vault-init-k8s.json \
  --recipient "$CLUSTER_BACKUP_AGE_RECIPIENT" --force
./platform-orchestrator/platform.sh restore-cluster \
  --archive /secure/k8s-cluster-....tar.gz.age --mode verify \
  --identity "$CLUSTER_BACKUP_AGE_IDENTITY"

# On a multi-cluster controller, bind the backup to this cluster's exact
# generated secret set and encrypted Vault initialization material.
./scripts/cluster-backup.sh \
  --config /state/cluster-a/platform.yaml \
  --secrets-file /state/cluster-a/.platform-secrets.yml \
  --vault-init-file /state/cluster-a/.vault-init-cluster-a.json \
  --ssh-known-hosts /state/cluster-a/ssh/known_hosts \
  --recipient "$CLUSTER_BACKUP_AGE_RECIPIENT" \
  --output-dir /secure/cluster-a --force
./scripts/cluster-restore.sh \
  --archive /secure/cluster-a/k8s-cluster-....tar.gz.age \
  --mode verify --identity "$CLUSTER_BACKUP_AGE_IDENTITY"

# Materialize exact state, then require the fresh schema-v2 receipt before the
# source project can be removed.
./scripts/cluster-restore.sh \
  --archive /secure/cluster-a/k8s-cluster-....tar.gz.age \
  --mode operator-state --identity "$CLUSTER_BACKUP_AGE_IDENTITY" \
  --output-dir /secure/recovery/cluster-a
./teardown.sh cluster-a --confirm cluster-a \
  --require-backup-receipt \
  /secure/cluster-a/k8s-cluster-....tar.gz.age.manifest.json
```

GitLab recovery needs the Toolbox archive and the separately stored Rails
encryption secret; its external PostgreSQL data comes from the independently
gated pgBackRest set, not the Toolbox archive. Vault recovery uses the
Ansible-Vault-encrypted initialization file included in the cluster bundle;
the Ansible Vault password remains a separately stored dependency. Production
recovery also needs external Velero/Kopia data and the encrypted
etcd/PKI/config bundle. Backup object existence is not restore proof; run all
five isolated component drills and replacement-cluster drills on a schedule. See
[BACKUP_RESTORE.md](BACKUP_RESTORE.md).

The receipt gate is evaluated before the first Hetzner API list or mutation. It
requires a recent schema-v2 receipt matching the exact project and live source
cluster UID, validates the local archive/checksum, then downloads and compares
the remote receipt, checksum, and archive through the configured DR endpoint.
The default maximum age is 24 hours; change it only with the recorded
`--max-backup-age-seconds` maintenance-window policy. Build the replacement
with the same logical identity and exact Velero prefix, use the dedicated
`velero-bootstrap` tag, then run strict Velero and native-catalog replay gates.

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
VictoriaMetrics single-to-cluster or cluster-to-single, validates the target,
and captures the second encrypted recovery point. Metrics migration injects a
persisted historical sentinel and requires its exact value and millisecond
timestamp from both topologies; finalization repeats the destination query
before old metrics PVC deletion. Before finalization, `migrate rollback` copies
post-switch VictoriaMetrics samples back, proves a post-switch delta sentinel on
both sides, then restores the
recorded Helm/config baseline, removes target-only components in dependency
order, and deliberately retains expanded/resized nodes. If Vault already uses
the migrated Raft storage, rollback restores every non-Vault Helm revision and
retains Raft. Target-only data removal is authorized only by the completed
post-migration backup checkpoint; without it rollback fails closed rather than
discarding target writes. HIPAA-oriented hardening is never generically
reversed.
Finalization is itself checkpointed. Before every invocation with destructive
work still pending, it refreshes and verifies the final encrypted recovery
point. It then removes disabled dependants, retires old metrics/logging PVCs,
removes excess workers then control planes through Kubespray, reconciles the
exact target, removes disabled backup resources near the end, and cleans unused
cloud placement resources.

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

The confirmation must match the project. Servers, load balancers, firewalls,
and labeled placement groups are selected only by an exact `project` label;
overlapping name prefixes never broaden teardown scope. Exact conventional
names are used for the SSH key, subnet, network, and legacy spread group. The
script also verifies deletion of every CSI volume captured by attachment to
those exact labeled servers. It reads detached volume handles from PVs only
when every node in the active Kubernetes context is an exact member of the
provider-server set. It preserves DNS and the global kubeconfig. The
orchestrator passes the configured
`k8s_api_local_port`, so teardown stops only that project's matching tunnel and
removes only its project-specific known-hosts file. Review the backup inventory
before authorizing teardown.
