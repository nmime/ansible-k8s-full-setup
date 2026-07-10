# Upgrade Runbook

## Overview

This runbook covers the full lifecycle of upgrading the Kubernetes platform
managed by this Ansible setup. It uses a phased, canary approach with automated
health gates, snapshot-based rollback, and dry-run simulation.

## Architecture

```
+--------------------------------------------------------------+
|           upgrade-platform.sh (orchestrator)                 |
|  +----------+  +----------+  +----------+  +-----------+    |
|  |preflight |->|snapshot  |->|canary    |->|health     |    |
|  |-check.py |  |-helm-    |  |phases    |  |gates      |    |
|  |           |  |baseline  |  |          |  |           |    |
|  +----------+  +----------+  +----------+  +-----------+    |
|                     ^                  v                      |
|                     |              rollback.sh                |
|                  snapshot/                        .state/     |
+--------------------------------------------------------------+
```

## Prerequisites

- `kubectl`, `helm`, `yq`, `ansible-playbook` installed and in PATH
- Cluster accessible (`kubectl cluster-info` works)
- `platform.yaml` configured with valid `global.domain` and `global.email`
- Sufficient disk space (< 80% usage recommended)

## Tier Upgrade Order (Canary Phases)

Upgrades always progress through the tier sequence from `minimal` to the
target tier:

| Phase | Tier       | Nodes | HA  | Purpose                       |
|-------|------------|-------|-----|-------------------------------|
| 1     | `minimal`  | 2     | No  | Smoke test - smallest cluster  |
| 2     | `small`    | 3     | No  | Validate multi-worker upgrade  |
| 3     | `medium`   | 5     | Yes | Validate HA configuration      |
| 4     | `production`| 6    | Yes | Full production validation     |

Each phase runs a full health gate check before advancing.

## Quick Reference

```bash
# Plan what will change (dry run)
./scripts/upgrade-platform.sh --dry-run plan

# Run preflight checks
./scripts/upgrade-platform.sh preflight

# Capture baseline snapshot
./scripts/upgrade-platform.sh snapshot

# Dry-run full upgrade
./scripts/upgrade-platform.sh --dry-run execute

# Execute upgrade
./scripts/upgrade-platform.sh --tier medium execute

# Upgrade specific component
./scripts/upgrade-platform.sh --tier small --component argocd execute

# Validate cluster health
./scripts/upgrade-platform.sh validate

# Rollback all components
./scripts/rollback.sh

# Rollback specific component
./scripts/rollback.sh --component argocd

# Dry-run rollback
./scripts/rollback.sh --dry-run
```

## Step-by-Step: Full Tier Upgrade

### Step 1: Pre-Upgrade Preparation

```bash
./scripts/upgrade-platform.sh --dry-run plan
./scripts/upgrade-platform.sh preflight
```

### Step 2: Capture Baseline Snapshot

```bash
./scripts/upgrade-platform.sh snapshot
ls -la snapshot/
cat snapshot/latest/MANIFEST.yaml
```

### Step 3: Execute Upgrade

```bash
./scripts/upgrade-platform.sh --tier medium execute
# Or force without confirmation:
./scripts/upgrade-platform.sh --tier medium --force execute
```

The upgrade will:
1. Run preflight checks
2. Capture a pre-upgrade snapshot
3. Progress through canary phases (minimal -> small -> medium)
4. Run health gates after each phase
5. Run final health gates

If any phase fails, automatic rollback is initiated.

### Step 4: Post-Upgrade Validation

```bash
./scripts/upgrade-platform.sh validate
kubectl get pods -n argocd
kubectl get pods -n cert-manager
helm list --all-namespaces
```

## Component-Specific Upgrades

```bash
./scripts/upgrade-platform.sh --component argocd --tier small execute
./scripts/upgrade-platform.sh --component cilium --tier small execute
./scripts/upgrade-platform.sh --component argocd --component cert-manager --tier small execute
```

Supported components: `argocd`, `cilium`, `cert-manager`, `database`, `observability`, `gitlab`.

## Health Gates

| Gate          | Check                                         | Fatal? |
|---------------|-----------------------------------------------|--------|
| Nodes         | All nodes in `Ready` state                    | Yes    |
| Cilium        | All Cilium pods `Running` in `kube-system`    | Yes    |
| Cert-manager  | All cert-manager pods `Running`               | Yes    |
| ArgoCD        | All ArgoCD pods `Running` in `argocd`         | Yes    |
| Databases     | PostgreSQL and MongoDB pods `Running`         | Yes    |

Non-deployed components produce a warning (non-fatal).

## Rollback Procedures

### Automatic Rollback

If any canary phase or final health gate fails, `rollback.sh` is invoked automatically.

### Manual Rollback

```bash
./scripts/rollback.sh                    # Full rollback
./scripts/rollback.sh --component argocd  # Component-specific
./scripts/rollback.sh --dry-run          # Simulate
./scripts/rollback.sh --force            # Skip confirmation
```

## Dry-Run Mode

Add `--dry-run` to any command:
```bash
./scripts/upgrade-platform.sh --dry-run execute
./scripts/rollback.sh --dry-run --component argocd
```

## Preflight Checks

| Check              | Description                                |
|--------------------|--------------------------------------------|
| `tool:kubectl`     | kubectl binary available                   |
| `tool:helm`        | helm binary available                      |
| `tool:yq`          | yq binary available                        |
| `tool:ansible`     | ansible-playbook available                 |
| `cluster:connect`  | kubectl can reach cluster                  |
| `cluster:version`  | Server version detectable                  |
| `helm:health`      | No failing Helm releases                   |
| `nodes:ready`      | All nodes Ready                            |
| `disk:space`       | Disk usage < 80%                           |
| `snapshot:exists`  | At least one snapshot available            |
| `git:clean`        | Working tree clean (warning only)          |
| `config:domain`    | global.domain set                          |
| `config:email`     | global.email set                           |

Skip with `--skip-preflight` (not recommended).

## Snapshot Contents

Each snapshot captures:
- `helm/all-releases.yaml` - all helm releases
- `helm/<namespace>.yaml` - per-namespace details
- `helm-values/<ns>-<release>.yaml` - stored values
- `crds.yaml` - custom resource definitions
- `namespaces.yaml` - all namespaces
- `rbac/` - cluster roles and bindings
- `pvs.yaml` / `pvcs.yaml` - persistent storage
- `nodes.yaml` / `version.txt` - cluster info
- `MANIFEST.yaml` - snapshot metadata

## State Files

The `.upgrade-state/` directory tracks:
- `canary-<tier>.json` - status of each canary phase
- `upgrade-complete.json` - final upgrade result
- `rollback-complete.json` - rollback result

## Troubleshooting

### Preflight fails on tool checks
Ensure binaries are installed:
```bash
curl -LO "https://dl.k8s.io/release/$(curl -L -s https://dl.k8s.io/release/stable.txt)/bin/linux/amd64/kubectl"
sudo install -o root -g root -m 0755 kubectl /usr/local/bin/kubectl
curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
pip install yq ansible
```

### Health gate fails for non-deployed component
Non-deployed components produce warnings, not errors.

### Rollback fails
1. Verify snapshot: `ls -la snapshot/`
2. Check content: `cat snapshot/latest/MANIFEST.yaml`
3. Try component-specific rollback first
4. Manually re-run ansible:
   ```bash
   ansible-playbook playbooks/deploy_platform.yml -e "tier=<previous>" -e "domain=<domain>" -e "email=<email>"
   ```

### Snapshot capture fails
1. Verify connectivity: `kubectl cluster-info`
2. Verify helm: `helm list --all-namespaces`
3. Check permissions
4. Run with `--verbose`

## CI/CD Integration

```bash
./scripts/upgrade-platform.sh --dry-run plan
./scripts/upgrade-platform.sh preflight
./scripts/upgrade-platform.sh --tier medium --force execute
./scripts/upgrade-platform.sh validate
```

Each command exits with appropriate codes (0 = success, 1 = failure).

## Maintenance

- Clean old snapshots: `find snapshot/ -name 'upgrade-*' -mtime +30 -exec rm -rf {} +`
- Review state: `cat .upgrade-state/upgrade-complete.json`
- Audit health: `./scripts/health-gates.sh`
