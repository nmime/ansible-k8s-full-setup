# Validation and CI

The repository has one active workflow: `.github/workflows/ci.yml`. It runs on
pull requests and pushes to `main` with read-only repository permissions.
Third-party setup actions are pinned to immutable commit SHAs.

CI installs the pinned Ansible collections, parses all four operational
entry-point playbooks, and then runs the same mandatory suite used locally:

```bash
bash scripts/validate-local.sh
```

The suite fails when a required tool is missing. It runs:

1. `yamllint`
2. all pre-commit hooks
3. `ansible-lint`
4. `shellcheck` over every repository shell script
5. the version compatibility matrix
6. `ansible-playbook --syntax-check` for deployment, removal, post-Kubespray
   continuation, and edge-CDN playbooks
7. the complete pytest unit and static component-contract suite

Install local dependencies before running it:

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
bash scripts/validate-local.sh
```

Cluster-changing checks and destructive restore drills are intentionally not
run on shared CI runners. Run their explicit dry-run modes locally first, then
execute them against an authorized test cluster during a maintenance window.
The live acceptance sequence is `scripts/health-gates.sh` followed by
`scripts/live-tier-smoke.sh`: the first enforces full controller, storage,
certificate, route, Helm, and runtime-security readiness; the second validates
the selected profile with temporary service-level read/write sentinels and
tests every selected Gateway API TLS route from inside the cluster using its
declared hostname, Gateway address, and listener port. This also covers
VPN-only admin routes without weakening their intended public isolation.
