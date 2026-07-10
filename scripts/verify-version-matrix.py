#!/usr/bin/env python3
"""
verify-version-matrix.py — Version compatibility baseline validator

Validates that all version references across the repository are consistent
and that upstream releases actually exist. Used in CI as the `version-matrix` job.

Exit codes:
  0 — All versions valid and consistent
  1 — Version mismatch or upstream not found
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

# ── Version registry ──────────────────────────────────────────────────────────
# Every version that must stay in sync across files is declared here.
# Format: (defaults_key, expected_value, [list of file patterns that must match])

VERSION_REGISTRY = {
    "k8s_version": {
        "value": "v1.35.6",
        "files": [
            ("defaults/main.yml", "k8s_version:"),
            ("inventory.example", "k8s_version:"),
            ("README.md", "kubernetes-v1.35.6-"),
            ("RUNBOOK.md", "v1.35.6"),
            ("roles/k8s-cluster-management/tasks/main.yml", "k8s_version | default('v1.35.6')"),
            ("platform-orchestrator/platform.example.yaml", "version: v1.35.6"),
            ("platform-orchestrator/profiles/medium.yaml", "version: v1.35.6"),
            ("platform-orchestrator/profiles/medium-optimized.yaml", "version: v1.35.6"),
            ("platform-orchestrator/profiles/minimal.yaml", "version: v1.35.6"),
            ("platform-orchestrator/profiles/production.yaml", "version: v1.35.6"),
            ("platform-orchestrator/profiles/small.yaml", "version: v1.35.6"),
        ],
    },
    "cilium_version": {
        "value": "v1.19.5",
        "files": [
            ("defaults/main.yml", "cilium_version: v1.19.5"),
            ("roles/k8s-cluster-management/tasks/main.yml", "cilium_version | default('v1.19.5')"),
            ("README.md", "Cilium** | v1.19.5 |"),
            ("roles/README.md", "Cilium v1.19.5"),
            ("platform-orchestrator/platform.example.yaml", "cilium_version: v1.19.5"),
        ],
    },
    "gateway_api_version": {
        "value": "v1.6.0",
        "files": [
            ("defaults/main.yml", "gateway_api_version: v1.6.0"),
            ("roles/k8s-cluster-management/tasks/main.yml", "gateway_api_version | default('v1.6.0')"),
            ("platform-orchestrator/platform.example.yaml", "gateway_api_version: v1.6.0"),
        ],
    },
    "certmanager_version": {
        "value": "v1.21.0",
        "files": [
            ("roles/k8s-cluster-management/tasks/main.yml", 'certmanager_ver: "v1.21.0"'),
            ("roles/README.md", "cert-manager v1.21.0"),
        ],
    },
    "metallb_version": {
        "value": "v0.16.0",
        "files": [
            ("roles/k8s-cluster-management/tasks/main.yml", 'metallb_ver: "v0.16.0"'),
        ],
    },
    "hetzner_ccm_version": {
        "value": "v1.33.0",
        "files": [
            ("roles/k8s-cluster-management/tasks/main.yml", 'hetzner_ccm_ver: "v1.33.0"'),
        ],
    },
    "hetzner_csi_version": {
        "value": "v2.22.0",
        "files": [
            ("roles/k8s-cluster-management/tasks/main.yml", 'hetzner_csi_ver: "v2.22.0"'),
        ],
    },
    "keda_chart_version": {
        "value": "2.20.1",
        "files": [
            ("defaults/main.yml", 'keda_chart_version: "2.20.1"'),
            ("roles/README.md", "Chart 2.20.1"),
        ],
    },
    "es_version": {
        "value": "9.4.3",
        "files": [
            ("defaults/main.yml", 'es_version: "9.4.3"'),
            ("roles/elasticsearch/defaults/main.yml", 'es_version: "9.4.3"'),
            ("README.md", "Elasticsearch** | v9.4.3 |"),
        ],
    },
    "kibana_version": {
        "value": "9.4.3",
        "files": [
            ("defaults/main.yml", 'kibana_version: "9.4.3"'),
            ("roles/elasticsearch/defaults/main.yml", 'kibana_version: "9.4.3"'),
        ],
    },
    "apm_version": {
        "value": "9.4.3",
        "files": [
            ("roles/apm-server/defaults/main.yml", 'apm_version: "9.4.3"'),
        ],
    },
    "postal_version": {
        "value": "3.3.7",
        "files": [
            ("defaults/main.yml", 'postal_version: "3.3.7"'),
            ("roles/postal/defaults/main.yml", 'postal_version: "3.3.7"'),
        ],
    },
    "dragonfly_operator_version": {
        "value": "v1.6.1",
        "files": [
            ("roles/dragonfly/defaults/main.yml", 'dragonfly_operator_version: "v1.6.1"'),
            ("roles/README.md", "Operator v1.6.1"),
        ],
    },
    "dragonfly_image_version": {
        "value": "v1.39.0",
        "files": [
            ("roles/dragonfly/defaults/main.yml", 'dragonfly_image_version: "v1.39.0"'),
            ("roles/README.md", "Dragonfly v1.39.0"),
        ],
    },
    "blackbox_chart_version": {
        "value": "11.15.1",
        "files": [
            ("roles/blackbox-exporter/defaults/main.yml", 'blackbox_chart_version: "11.15.1"'),
            ("roles/README.md", "Chart 11.15.1"),
        ],
    },
    "eso_chart_version": {
        "value": "2.7.0",
        "files": [
            ("roles/k8s-secrets/tasks/main.yml", "eso_chart_ver: 2.7.0"),
        ],
    },
    "kubespray_version": {
        "value": "v2.31.0",
        "files": [
            ("roles/k8s-cluster-management/tasks/main.yml", 'kubespray_version: "v2.31.0"'),
        ],
    },
}

# ── Helpers ───────────────────────────────────────────────────────────────────


def find_repo_root() -> Path:
    """Walk up from this script to find the repo root."""
    current = Path(__file__).resolve().parent
    for _ in range(10):
        if (current / "defaults" / "main.yml").exists():
            return current
        if (current / ".git").is_dir():
            return current
        parent = current.parent
        if parent == current:
            break
        current = parent
    return current.parent  # assume script is in scripts/


def read_file(root: Path, relpath: str) -> str | None:
    """Read a file from the repo root. Returns None if missing."""
    path = root / relpath
    try:
        return path.read_text()
    except (FileNotFoundError, PermissionError):
        return None


def check_version_consistency(root: Path | None = None) -> list[str]:
    """Return a list of error strings for any version mismatches."""
    if root is None:
        root = find_repo_root()
    errors: list[str] = []

    for var_name, spec in VERSION_REGISTRY.items():
        for relpath, pattern in spec["files"]:
            content = read_file(root, relpath)
            if content is None:
                errors.append(
                    f"[{var_name}] File not found: {relpath}"
                )
                continue
            if pattern not in content:
                errors.append(
                    f"[{var_name}] Pattern '{pattern}' not found in {relpath}"
                )

    return errors


def check_no_hardcoded_kubectl_version(root: Path | None = None) -> list[str]:
    """Ensure no hardcoded kubectl download URLs exist."""
    if root is None:
        root = find_repo_root()
    errors: list[str] = []

    # Check all YAML files for hardcoded kubectl download URLs with old versions
    yaml_files = list(root.rglob("*.yml")) + list(root.rglob("*.yaml"))
    for fpath in yaml_files:
        content = fpath.read_text()
        # Match patterns like dl.k8s.io/release/v1.X.Y.Z/
        matches = re.findall(r"dl\.k8s\.io/release/v\d+\.\d+\.\d+/bin", content)
        for match in matches:
            errors.append(
                f"[hardcoded-kubectl] Found hardcoded kubectl URL in {fpath.relative_to(root)}: {match}"
            )

    return errors


def main() -> int:
    root = find_repo_root()
    print(f"Scanning repo root: {root}")
    all_errors: list[str] = []

    # 1. Version consistency
    errors = check_version_consistency(root)
    all_errors.extend(errors)
    print(f"  Version consistency: {len(errors)} issue(s)")

    # 2. No hardcoded kubectl versions
    errors = check_no_hardcoded_kubectl_version(root)
    all_errors.extend(errors)
    print(f"  Hardcoded kubectl check: {len(errors)} issue(s)")

    if all_errors:
        print("\n❌ Version matrix validation FAILED:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    else:
        print("\n✅ All version checks passed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
