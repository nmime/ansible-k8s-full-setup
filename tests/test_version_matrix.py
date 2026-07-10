#!/usr/bin/env python3
"""
test_version_matrix.py — Unit, component, and E2E tests for version baseline.

Tests:
  Unit:     Version format validation, cross-file consistency, hardcoded kubectl check
  Component: defaults/main.yml as single source of truth, profile consistency
  E2E:      verify-version-matrix.py script runs cleanly against the repo

Run:
  pytest tests/test_version_matrix.py -v
  pytest tests/test_version_matrix.py -v -m unit
  pytest tests/test_version_matrix.py -v -m component
  pytest tests/test_version_matrix.py -v -m e2e
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────

REPO_ROOT = Path(__file__).resolve().parent.parent


def read_file(relpath: str) -> str | None:
    """Read a file from the repo root. Returns None if missing."""
    p = REPO_ROOT / relpath
    try:
        return p.read_text()
    except (FileNotFoundError, PermissionError):
        return None


# ── Constants ─────────────────────────────────────────────────────────────────

# Canonical versions (single source of truth for tests)
CANONICAL = {
    "k8s_version": "v1.35.6",
    "cilium_version": "v1.19.5",
    "gateway_api_version": "v1.6.0",
    "certmanager_ver": "v1.21.0",
    "metallb_ver": "v0.16.0",
    "hetzner_ccm_ver": "v1.33.0",
    "hetzner_csi_ver": "v2.22.0",
    "keda_chart_version": "2.20.1",
    "es_version": "9.4.3",
    "kibana_version": "9.4.3",
    "apm_version": "9.4.3",
    "postal_version": "3.3.7",
    "dragonfly_operator_version": "v1.6.1",
    "dragonfly_image_version": "v1.39.0",
    "blackbox_chart_version": "11.15.1",
    "eso_chart_ver": "2.7.0",
    "kubespray_version": "v2.31.0",
}

VERSION_REGEX = re.compile(r"v?\d+\.\d+\.\d+")


# ──────────────────────────────────────────────────────────────────────────────
# UNIT TESTS
# ──────────────────────────────────────────────────────────────────────────────


class TestVersionFormats:
    """Unit: all canonical versions are valid semver-like strings."""

    @pytest.mark.parametrize("var,ver", CANONICAL.items())
    def test_version_is_semver(self, var: str, ver: str) -> None:
        match = VERSION_REGEX.fullmatch(ver)
        assert match is not None, f"{var}={ver!r} is not a valid version string"


class TestDefaultsMainYml:
    """Unit: defaults/main.yml contains all primary version variables."""

    @pytest.fixture(autouse=True)
    def _content(self) -> str:
        content = read_file("defaults/main.yml")
        assert content is not None, "defaults/main.yml not found"
        self.content: str = content

    def test_k8s_version(self) -> None:
        assert f"k8s_version: {CANONICAL['k8s_version']}" in self.content

    def test_cilium_version(self) -> None:
        assert f"cilium_version: {CANONICAL['cilium_version']}" in self.content

    def test_gateway_api_version(self) -> None:
        assert f"gateway_api_version: {CANONICAL['gateway_api_version']}" in self.content

    def test_keda_chart_version(self) -> None:
        assert f'keda_chart_version: "{CANONICAL["keda_chart_version"]}"' in self.content

    def test_es_version(self) -> None:
        assert f'es_version: "{CANONICAL["es_version"]}"' in self.content

    def test_kibana_version(self) -> None:
        assert f'kibana_version: "{CANONICAL["kibana_version"]}"' in self.content

    def test_postal_version(self) -> None:
        assert f'postal_version: "{CANONICAL["postal_version"]}"' in self.content


class TestK8sClusterManagementTasks:
    """Unit: k8s-cluster-management/tasks/main.yml has correct version defaults."""

    @pytest.fixture(autouse=True)
    def _content(self) -> str:
        path = "roles/k8s-cluster-management/tasks/main.yml"
        content = read_file(path)
        assert content is not None, f"{path} not found"
        self.content: str = content

    def test_k8s_ver_default(self) -> None:
        assert f"k8s_version | default('{CANONICAL['k8s_version']}')" in self.content

    def test_cilium_ver_default(self) -> None:
        assert f"cilium_version | default('{CANONICAL['cilium_version']}')" in self.content

    def test_gateway_api_ver_default(self) -> None:
        assert f"gateway_api_version | default('{CANONICAL['gateway_api_version']}')" in self.content

    def test_certmanager_ver(self) -> None:
        assert f'certmanager_ver: "{CANONICAL["certmanager_ver"]}"' in self.content

    def test_metallb_ver(self) -> None:
        assert f'metallb_ver: "{CANONICAL["metallb_ver"]}"' in self.content

    def test_hetzner_ccm_ver(self) -> None:
        assert f'hetzner_ccm_ver: "{CANONICAL["hetzner_ccm_ver"]}"' in self.content

    def test_hetzner_csi_ver(self) -> None:
        assert f'hetzner_csi_ver: "{CANONICAL["hetzner_csi_ver"]}"' in self.content

    def test_kubespray_version(self) -> None:
        assert f'kubespray_version: "{CANONICAL["kubespray_version"]}"' in self.content

    def test_header_comment_updated(self) -> None:
        """Header comment mentions correct versions."""
        assert CANONICAL["cilium_version"] in self.content.split("\n")[1]


class TestNoHardcodedKubectlVersion:
    """Unit: no hardcoded kubectl download URLs with literal version pins."""

    def test_no_hardcoded_kubectl_in_network_security(self) -> None:
        path = "roles/network-security/tasks/main.yml"
        content = read_file(path)
        assert content is not None, f"{path} not found"
        # Should use {{ k8s_version | default(...) }} not a literal version
        # Look for patterns like dl.k8s.io/release/v1.X.Y.Z
        matches = re.findall(r"dl\.k8s\.io/release/v\d+\.\d+\.\d+/bin", content)
        # If there are matches, they should only be inside Jinja expressions
        for match in matches:
            # The line containing the match should also contain k8s_version
            for line in content.split("\n"):
                if match in line:
                    assert "k8s_version" in line, (
                        f"Hardcoded kubectl version found: {match} (should use k8s_version variable)"
                    )

    def test_no_hardcoded_kubectl_in_any_task_file(self) -> None:
        """Check all task YAML files for hardcoded kubectl download URLs."""
        task_files = (
            list(REPO_ROOT.glob("roles/*/tasks/*.yml"))
            + list(REPO_ROOT.glob("roles/*/tasks/*.yaml"))
        )
        for fpath in task_files:
            content = fpath.read_text()
            matches = re.findall(r"dl\.k8s\.io/release/v\d+\.\d+\.\d+/bin", content)
            for match in matches:
                for line in content.split("\n"):
                    if match in line:
                        assert "k8s_version" in line, (
                            f"{fpath.relative_to(REPO_ROOT)}: hardcoded kubectl {match}"
                        )


class TestRoleDefaultsVersions:
    """Unit: individual role defaults match canonical versions."""

    def test_blackbox_chart_version(self) -> None:
        content = read_file("roles/blackbox-exporter/defaults/main.yml")
        assert content is not None
        assert f'blackbox_chart_version: "{CANONICAL["blackbox_chart_version"]}"' in content

    def test_dragonfly_operator_version(self) -> None:
        content = read_file("roles/dragonfly/defaults/main.yml")
        assert content is not None
        assert f'dragonfly_operator_version: "{CANONICAL["dragonfly_operator_version"]}"' in content

    def test_dragonfly_image_version(self) -> None:
        content = read_file("roles/dragonfly/defaults/main.yml")
        assert content is not None
        assert f'dragonfly_image_version: "{CANONICAL["dragonfly_image_version"]}"' in content

    def test_elasticsearch_es_version(self) -> None:
        content = read_file("roles/elasticsearch/defaults/main.yml")
        assert content is not None
        assert f'es_version: "{CANONICAL["es_version"]}"' in content

    def test_elasticsearch_kibana_version(self) -> None:
        content = read_file("roles/elasticsearch/defaults/main.yml")
        assert content is not None
        assert f'kibana_version: "{CANONICAL["kibana_version"]}"' in content

    def test_apm_server_version(self) -> None:
        content = read_file("roles/apm-server/defaults/main.yml")
        assert content is not None
        assert f'apm_version: "{CANONICAL["apm_version"]}"' in content

    def test_postal_version(self) -> None:
        content = read_file("roles/postal/defaults/main.yml")
        assert content is not None
        assert f'postal_version: "{CANONICAL["postal_version"]}"' in content

    def test_eso_chart_version_in_tasks(self) -> None:
        content = read_file("roles/k8s-secrets/tasks/main.yml")
        assert content is not None
        assert f"eso_chart_ver: {CANONICAL['eso_chart_ver']}" in content


class TestNoStaleVersionReferences:
    """Unit: old version strings do not appear in updated files."""

    STALE_VERSIONS = [
        "v1.35.4", "v1.19.4", "v1.5.1", "v1.20.2", "v0.15.3",
        "v1.31.0", "v2.21.0", "2.19.0", "9.4.1", "3.3.6",
        "v1.5.0", "v1.38.1", "11.10.0",
    ]

    UPDATED_FILES = [
        "defaults/main.yml",
        "inventory.example",
        "roles/k8s-cluster-management/tasks/main.yml",
        "roles/network-security/tasks/main.yml",
        "roles/blackbox-exporter/defaults/main.yml",
        "roles/dragonfly/defaults/main.yml",
        "roles/elasticsearch/defaults/main.yml",
        "roles/apm-server/defaults/main.yml",
        "roles/postal/defaults/main.yml",
        "roles/k8s-secrets/tasks/main.yml",
    ]

    @pytest.mark.parametrize("stale_ver", STALE_VERSIONS)
    def test_no_stale_version_in_updated_files(self, stale_ver: str) -> None:
        for relpath in self.UPDATED_FILES:
            content = read_file(relpath)
            if content is None:
                continue
            assert stale_ver not in content, (
                f"Stale version {stale_ver!r} still present in {relpath}"
            )


# ──────────────────────────────────────────────────────────────────────────────
# COMPONENT TESTS
# ──────────────────────────────────────────────────────────────────────────────


class TestProfileConsistency:
    """Component: all platform profiles use the same k8s_version."""

    PROFILE_PATHS = [
        "platform-orchestrator/profiles/medium.yaml",
        "platform-orchestrator/profiles/medium-optimized.yaml",
        "platform-orchestrator/profiles/minimal.yaml",
        "platform-orchestrator/profiles/production.yaml",
        "platform-orchestrator/profiles/small.yaml",
    ]

    @pytest.mark.parametrize("profile_path", PROFILE_PATHS)
    def test_profile_k8s_version(self, profile_path: str) -> None:
        content = read_file(profile_path)
        assert content is not None, f"{profile_path} not found"
        expected = f"version: {CANONICAL['k8s_version']}"
        assert expected in content, (
            f"{profile_path}: expected k8s version {expected}"
        )


class TestInventoryExampleConsistency:
    """Component: inventory.example matches defaults/main.yml for shared vars."""

    def test_k8s_version_matches(self) -> None:
        defaults = read_file("defaults/main.yml")
        inventory = read_file("inventory.example")
        assert defaults is not None and inventory is not None

        # Extract k8s_version from defaults
        match = re.search(r"k8s_version:\s+(v?\d+\.\d+\.\d+)", defaults)
        assert match, "k8s_version not found in defaults/main.yml"
        ver = match.group(1)

        assert f"k8s_version: {ver}" in inventory, (
            f"inventory.example has mismatched k8s_version (expected {ver})"
        )


class TestReadmeVersionTable:
    """Component: README.md version table is consistent with defaults."""

    @pytest.fixture(autouse=True)
    def _content(self) -> str:
        content = read_file("README.md")
        assert content is not None, "README.md not found"
        self.content: str = content

    def test_k8s_badge(self) -> None:
        ver = CANONICAL["k8s_version"].replace("v", "v")
        assert f"kubernetes-{ver}-" in self.content

    def test_k8s_table_entry(self) -> None:
        assert f"| **Kubernetes** | {CANONICAL['k8s_version']} |" in self.content

    def test_cilium_table_entry(self) -> None:
        assert f"| **Cilium** | {CANONICAL['cilium_version']} |" in self.content

    def test_gateway_api_table_entry(self) -> None:
        # Gateway API might use short form (v1.6 instead of v1.6.0)
        assert "Gateway API** | v1.6" in self.content

    def test_cert_manager_table_entry(self) -> None:
        assert f"| **cert-manager** | {CANONICAL['certmanager_ver']} |" in self.content

    def test_metallb_table_entry(self) -> None:
        assert "MetalLB** | v0.16" in self.content

    def test_elasticsearch_table_entry(self) -> None:
        assert f"| **Elasticsearch** | v{CANONICAL['es_version']} |" in self.content


class TestRolesReadmeConsistency:
    """Component: roles/README.md version column is consistent."""

    @pytest.fixture(autouse=True)
    def _content(self) -> str:
        content = read_file("roles/README.md")
        assert content is not None, "roles/README.md not found"
        self.content: str = content

    def test_k8s_cluster_mgmt_versions(self) -> None:
        assert CANONICAL["k8s_version"] in self.content
        assert CANONICAL["cilium_version"] in self.content
        assert CANONICAL["certmanager_ver"] in self.content

    def test_dragonfly_versions(self) -> None:
        assert CANONICAL["dragonfly_operator_version"] in self.content
        assert CANONICAL["dragonfly_image_version"] in self.content

    def test_keda_version(self) -> None:
        assert CANONICAL["keda_chart_version"] in self.content

    def test_blackbox_version(self) -> None:
        assert CANONICAL["blackbox_chart_version"] in self.content

    def test_apm_version(self) -> None:
        assert CANONICAL["apm_version"] in self.content


# ──────────────────────────────────────────────────────────────────────────────
# E2E TESTS
# ──────────────────────────────────────────────────────────────────────────────


class TestVerifyScriptE2E:
    """E2E: verify-version-matrix.py runs successfully against the repo."""

    def test_verify_script_exits_zero(self) -> None:
        script = REPO_ROOT / "scripts" / "verify-version-matrix.py"
        assert script.exists(), "scripts/verify-version-matrix.py not found"
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, (
            f"verify-version-matrix.py failed:\nSTDOUT: {result.stdout}\nSTDERR: {result.stderr}"
        )

    def test_verify_script_output_mentions_pass(self) -> None:
        script = REPO_ROOT / "scripts" / "verify-version-matrix.py"
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert "All version checks passed" in result.stdout
