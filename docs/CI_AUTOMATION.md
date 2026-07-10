# CI Automation

This document describes the CI/CD automation for the ansible-k8s-full-setup platform infrastructure.

## Overview

The CI pipeline validates all infrastructure code before changes are merged. It runs on every push to `main`, `upgrade/*`, and `feature/*` branches, and on all pull requests targeting `main`.

## CI Jobs

| Job | Purpose | Tool |
|-----|---------|------|
| `lint-yaml` | YAML syntax & style validation | [yamllint](https://yamllint.readthedocs.io/) |
| `lint-ansible` | Ansible best practices & anti-patterns | [ansible-lint](https://ansible-lint.readthedocs.io/) |
| `ansible-syntax` | Playbook syntax validation | `ansible-playbook --syntax-check` |
| `shellcheck` | Shell script static analysis | [ShellCheck](https://www.shellcheck.net/) |
| `python-tests` | Unit, component, and E2E tests | [pytest](https://pytest.org/) |
| `version-matrix` | Version compatibility validation | Custom Python script |
| `trivy-secret-scan` | Secret leak detection | [Trivy](https://trivy.dev/) |

## Workflow Files

- `.github/workflows/ci.yml` – Main CI pipeline (all jobs above)
- `.github/workflows/trivy.yml` – Scheduled Trivy security scans (weekly)

## Configuration Files

| File | Purpose |
|------|---------|
| `.yamllint.yaml` | yamllint rules (extends default, relaxed line-length) |
| `.ansible-lint.yml` | ansible-lint rules (production profile, skipped safe rules) |
| `.pre-commit-config.yaml` | Pre-commit hooks (run locally before committing) |
| `.renovaterc.json` | Renovate auto-update configuration |
| `requirements.txt` | Pinned Python dependencies |

## Pre-Commit Hooks

Install and enable:

```bash
pip install pre-commit
pre-commit install
```

Hooks run on every `git commit`:

1. **pre-commit-hooks** – whitespace, merge conflicts, large files, private keys
2. **yamllint** – YAML linting
3. **shellcheck** – Shell script linting
4. **ansible-lint** – Ansible playbook/role linting
5. **flake8** – Python test linting
6. **gitleaks** – Secret detection

Override: `git commit --no-verify`

## Renovate Auto-Updates

[Renovate](https://docs.renovatebot.com/) is configured to:

- Run weekly on Mondays
- Group Helm chart updates together
- Group Python dependency updates together
- Group GitHub Actions updates together
- Auto-detect version variables in `defaults/main.yml` via `# renovate:` markers

### Version Variable Markers

Version variables in `defaults/main.yml` use inline comments for Renovate:

```yaml
# renovate: datasource=helm depName=gitlab
gitlab_chart_version: "9.11.4"

# renovate: datasource=github-releases depName=kubernetes/kubernetes
k8s_version: v1.35.6

# renovate: datasource=docker depName=elasticsearch
es_version: "9.4.3"
```

Datasources used:
- `helm` – Helm chart versions (GitLab, ArgoCD, KEDA, Temporal, SeaweedFS, Daytona)
- `github-releases` – GitHub release versions (Kubernetes, Cilium, Gateway API, ArgoCD CLI, Postal)
- `docker` – Docker image versions (Elasticsearch, Kibana)

## Trivy Security Scanning

### CI Scan (on every PR)
- Scans the repository filesystem for known vulnerability patterns
- Exits with code 0 (informational only – no auto-fail)

### Scheduled Scan (weekly, Monday 06:00 UTC)
- Full filesystem scan for CRITICAL/HIGH severities
- Configuration misconfiguration scan
- Results uploaded to GitHub Security tab as SARIF

## Running Tests Locally

```bash
# Install dependencies
pip install -r requirements.txt

# Run all tests
pytest tests/ -v

# Run specific test category
pytest tests/ -v -m unit        # Unit tests only
pytest tests/ -v -m component   # Component tests only
pytest tests/ -v -m e2e         # End-to-end tests only

# Run version matrix validation
python3 tests/test_version_matrix.py
```

## Test Structure

```
tests/
├── unit/                          # Fast, isolated tests
│   ├── test_defaults.py           # defaults/main.yml structure & versions
│   └── test_version_matrix.py     # Version format & consistency
├── component/                     # Multi-unit integration tests
│   ├── test_playbook_structure.py # Playbooks, roles, interconnections
│   └── test_ci_config.py          # CI config cross-validation
└── e2e/                          # Full pipeline simulation
    └── test_ci_pipeline.py        # CI pipeline simulation
```

## Branch Protection

Recommended rules for `main`:
- Require CI checks to pass (`ci` workflow)
- Require pull request reviews (minimum 1)
- Require status checks: `lint-yaml`, `lint-ansible`, `ansible-syntax`, `shellcheck`, `python-tests`, `version-matrix`

## Adding New Version Variables

1. Add the variable to `defaults/main.yml` with a `# renovate:` comment above it
2. Add a test assertion in `tests/unit/test_defaults.py` → `TestDefaultsVersions`
3. If it's a chart version, add a Helm regex manager entry if needed
4. Verify: `pytest tests/unit/test_defaults.py -v`

## Troubleshooting

### ansible-lint false positives
Add to `.ansible-lint.yml` `skip_list` or `warn_list`.

### yamllint line-length issues
The config allows 200 chars. For longer lines, add to `ignore` section in `.yamllint.yaml`.

### Pre-commit hook failures
Run `pre-commit run --all-files` to see all issues. Fix and re-commit.

### Renovate not detecting a version
1. Verify the `# renovate:` comment is on the line immediately before the variable
2. Check `.renovaterc.json` `fileMatch` includes the file path
3. Verify the `matchStrings` regex matches the variable format
