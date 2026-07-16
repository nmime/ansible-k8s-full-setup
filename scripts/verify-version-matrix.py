#!/usr/bin/env python3
"""
verify-version-matrix.py - Version compatibility baseline validator

Validates that all version references across the repository are consistent
and that no stale versions remain. Used in CI as the `version-matrix` job.

Exit codes:
  0 - All versions valid and consistent
  1 - Version mismatch or stale version found
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

# - Version registry -
VERSION_REGISTRY = {
    "k8s_version": {
        "value": "v1.35.6",
        "files": [
            ("defaults/main.yml", "k8s_version: v1.35.6"),
            ("inventory.example", "k8s_version: v1.35.6"),
            ("roles/k8s-cluster-management/tasks/main.yml", "k8s_version | default('v1.35.6')"),
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
        ],
    },
    "gateway_api_version": {
        "value": "v1.6.0",
        "files": [
            ("defaults/main.yml", "gateway_api_version: v1.6.0"),
            ("roles/k8s-cluster-management/tasks/main.yml", "gateway_api_version | default('v1.6.0')"),
        ],
    },
    "cert_manager_version": {
        "value": "v1.21.0",
        "files": [
            ("defaults/main.yml", "cert_manager_version: v1.21.0"),
            ("roles/k8s-cluster-management/tasks/main.yml", "cert_manager_version | default('v1.21.0')"),
        ],
    },
    "metallb_version": {
        "value": "v0.16.1",
        "files": [
            ("defaults/main.yml", "metallb_version: v0.16.1"),
            ("roles/k8s-cluster-management/tasks/main.yml", "metallb_version | default('v0.16.1')"),
        ],
    },
    "hetzner_ccm_version": {
        "value": "v1.33.0",
        "files": [
            ("defaults/main.yml", "hetzner_ccm_version: v1.33.0"),
            ("roles/k8s-cluster-management/tasks/main.yml", "hetzner_ccm_version | default('v1.33.0')"),
        ],
    },
    "hetzner_csi_version": {
        "value": "v2.22.0",
        "files": [
            ("defaults/main.yml", "hetzner_csi_version: v2.22.0"),
            ("roles/k8s-cluster-management/tasks/main.yml", "hetzner_csi_version | default('v2.22.0')"),
        ],
    },
    "keda_chart_version": {
        "value": "2.20.1",
        "files": [
            ("defaults/main.yml", 'keda_chart_version: "2.20.1"'),
        ],
    },
    "es_version": {
        "value": "9.4.3",
        "files": [
            ("defaults/main.yml", 'es_version: "9.4.3"'),
            ("roles/elasticsearch/defaults/main.yml", 'es_version: "9.4.3"'),
        ],
    },
    "kibana_version": {
        "value": "9.4.3",
        "files": [
            ("defaults/main.yml", 'kibana_version: "9.4.3"'),
            ("roles/elasticsearch/defaults/main.yml", 'kibana_version: "9.4.3"'),
        ],
    },
    "apm_server_version": {
        "value": "9.4.3",
        "files": [
            ("defaults/main.yml", 'apm_server_version: "9.4.3"'),
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
    "blackbox_chart_version": {
        "value": "11.15.1",
        "files": [
            ("defaults/main.yml", 'blackbox_chart_version: "11.15.1"'),
            ("roles/blackbox-exporter/defaults/main.yml", 'blackbox_chart_version: "11.15.1"'),
        ],
    },
    "dragonfly_operator_version": {
        "value": "v1.6.1",
        "files": [
            ("defaults/main.yml", 'dragonfly_operator_version: "v1.6.1"'),
            ("roles/dragonfly/defaults/main.yml", 'dragonfly_operator_version: "v1.6.1"'),
        ],
    },
    "dragonfly_image_version": {
        "value": "v1.39.0",
        "files": [
            ("defaults/main.yml", 'dragonfly_image_version: "v1.39.0"'),
            ("roles/dragonfly/defaults/main.yml", 'dragonfly_image_version: "v1.39.0"'),
        ],
    },
    "vm_operator_version": {
        "value": "0.66.2",
        "files": [
            ("defaults/main.yml", 'vm_operator_version: "0.66.2"'),
        ],
    },
    "pmm_server_version": {
        "value": "3.8.1",
        "files": [
            ("defaults/main.yml", 'pmm_server_version: "3.8.1"'),
            ("roles/k8s-observability/tasks/main.yml", "percona/pmm-server:{{ pmm_server_version }}"),
        ],
    },
    "vault_version": {
        "value": "2.0.3",
        "files": [
            ("defaults/main.yml", 'vault_version: "2.0.3"'),
            ("roles/k8s-secrets/tasks/main.yml", "tag: '{{ vault_version }}'"),
        ],
    },
    "caddy_image_tag": {
        "value": "2.11.4-alpine",
        "files": [
            ("defaults/main.yml", 'caddy_image_tag: "2.11.4-alpine"'),
            ("roles/network-security/tasks/main.yml", "image: caddy:{{ caddy_image_tag }}"),
        ],
    },
    "coroot_versions": {
        "value": "pinned",
        "files": [
            ("defaults/main.yml", 'coroot_operator_chart_version: "0.9.7"'),
            ("defaults/main.yml", 'coroot_chart_version: "0.3.3"'),
            ("defaults/main.yml", 'coroot_image_tag: "1.23.3"'),
            ("defaults/main.yml", 'coroot_node_agent_image_tag: "1.34.2"'),
            ("defaults/main.yml", 'coroot_cluster_agent_image_tag: "1.7.1"'),
            ("defaults/main.yml", 'coroot_clickhouse_image_tag: "25.11.2-ubi9-0"'),
            ("roles/k8s-observability/tasks/coroot.yml", "chart_ref: coroot/coroot-operator"),
            ("roles/k8s-observability/tasks/coroot.yml", "chart_ref: coroot/coroot-ce"),
        ],
    },
    "eso_chart_version": {
        "value": "2.7.0",
        "files": [
            ("defaults/main.yml", 'eso_chart_version: "2.7.0"'),
        ],
    },
    "kubespray_version": {
        "value": "v2.31.0",
        "files": [
            ("defaults/main.yml", "kubespray_version: v2.31.0"),
        ],
    },
    "hcloud_cli_version": {
        "value": "v1.65.0",
        "files": [
            ("defaults/main.yml", "hcloud_cli_version: v1.65.0"),
        ],
    },
    "yq_version": {
        "value": "v4.44.6",
        "files": [
            ("defaults/main.yml", "yq_version: v4.44.6"),
        ],
    },
}

STALE_VERSIONS = [
    "v1.35.4", "v1.19.4", "v1.5.1", "v1.20.2",
    "v0.15.3", "v1.31.0", "v2.21.0", "v1.42.0",
    "2.19.0", "9.4.1", "3.3.6",
    "11.10.0", "v1.5.0", "v1.38.1",
    "0.59.3", "2.5.0",
]


def find_repo_root() -> Path:
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
    return current.parent


def read_file(root: Path, relpath: str) -> str | None:
    try:
        return (root / relpath).read_text()
    except (FileNotFoundError, PermissionError):
        return None


def check_version_consistency(root: Path) -> list[str]:
    errors = []
    for var_name, spec in VERSION_REGISTRY.items():
        for relpath, pattern in spec["files"]:
            content = read_file(root, relpath)
            if content is None:
                errors.append(f"[{var_name}] File not found: {relpath}")
                continue
            if pattern not in content:
                errors.append(f"[{var_name}] Pattern '{pattern}' not found in {relpath}")
    return errors


def check_no_stale_versions(root: Path) -> list[str]:
    errors = []
    for yml in list(root.rglob("roles/**/*.yml")) + list(root.rglob("roles/**/*.yaml")):
        content = yml.read_text()
        for stale in STALE_VERSIONS:
            if stale in content:
                errors.append(f"[stale] '{stale}' found in {yml.relative_to(root)}")
    return errors


def check_no_latest_tags(root: Path) -> list[str]:
    errors = []
    for yml in list(root.rglob("roles/**/*.yml")) + list(root.rglob("roles/**/*.yaml")):
        content = yml.read_text()
        clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', content)
        if "releases/latest/" in clean:
            errors.append(f"[latest-url] /latest/ URL in {yml.relative_to(root)}")
        for i, line in enumerate(content.splitlines(), 1):
            s = line.strip()
            if "pod-security.kubernetes.io" in s:
                continue
            if re.search(r'[\s"]:\s*latest\s*$|:latest[\s"}]', s):
                errors.append(f"[latest-tag] :latest tag in {yml.relative_to(root)}:{i}")
                break
    return errors


def check_kubectl_uses_variable(root: Path) -> list[str]:
    errors = []
    ns = root / "roles" / "network-security" / "tasks" / "main.yml"
    if not ns.exists():
        return []
    c = ns.read_text()
    if "dl.k8s.io/release/v1.35" in c and "${K8S_VER}" not in c:
        errors.append("[kubectl] hardcoded K8s version found")
    if "${K8S_VER}" not in c:
        errors.append("[kubectl] should use ${K8S_VER} variable")
    return errors


def check_cli_pinned(root: Path) -> list[str]:
    errors = []
    ns = root / "roles" / "network-security" / "tasks" / "main.yml"
    if not ns.exists():
        return []
    c = ns.read_text()
    clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', c)
    if "releases/latest/" in clean:
        errors.append("[cli] unpinned /latest/ URL")
    if "${HCLOUD_VER}" not in c:
        errors.append("[cli] hcloud should use ${HCLOUD_VER}")
    if "${YQ_VER}" not in c:
        errors.append("[cli] yq should use ${YQ_VER}")
    return errors


def main() -> int:
    root = find_repo_root()
    print(f"Scanning repo root: {root}")
    all_errors = []

    checks = [
        ("Version consistency", check_version_consistency),
        ("No stale versions", check_no_stale_versions),
        ("No :latest tags", check_no_latest_tags),
        ("kubectl uses variable", check_kubectl_uses_variable),
        ("CLI tools pinned", check_cli_pinned),
    ]

    for name, fn in checks:
        errors = fn(root)
        all_errors.extend(errors)
        status = f"FAIL ({len(errors)})" if errors else "OK"
        print(f"  [{status:>10}] {name}")

    if all_errors:
        print(f"\nVersion matrix validation FAILED:")
        for err in all_errors:
            print(f"  {err}")
        return 1
    else:
        print("\nAll version checks passed.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
