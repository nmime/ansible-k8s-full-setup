# Validation Tests

Run the canonical local/CI suite from the repository root:

```bash
python3 -m pip install -r requirements.txt
ansible-galaxy collection install -r requirements.yml
bash scripts/validate-local.sh
```

The suite fails when mandatory tooling is missing. It runs YAML lint,
pre-commit hooks, Ansible lint, ShellCheck, the pinned version matrix,
entry-point playbook syntax checks, and all pytest suites.

Pytest coverage includes:

- profile identity, explicit technology flags, dependency failures, resource
  tier propagation, lifecycle commands, and guarded removal;
- Coroot operator/chart/image pins, external VictoriaMetrics wiring, scoped
  privileged admission, compact medium-optimized sizing, and rollout waits;
- HIPAA-oriented selector dependencies and active Promtail/Filebeat/Fluentd
  redaction configuration;
- Elasticsearch Basic-license integrity and security resources;
- GitLab, PostgreSQL, and Vault upgrade contracts;
- backup/restore safety gates;
- bounded profile-aware HTTP/S3/PostgreSQL/Vault/Dragonfly load planning,
  pinned probe images, cleanup traps, thresholds, and secret-free JSON/TSV
  evidence contracts;
- validation-script fail-closed behavior and version compatibility.

Individual suites can be run with `pytest tests/test_platform_profiles.py -q`
or another selected test file. Counts are intentionally not hard-coded here;
the pytest result is the current source of truth.

These tests are static/parser/unit checks. They do not prove a live Hetzner
deployment, Coroot eBPF compatibility, restore, or upgrade. Those operations
need an explicitly authorized disposable cluster and recorded runtime evidence.
