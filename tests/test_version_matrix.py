#!/usr/bin/env python3
"""Tests for version baseline upgrade: unit, component, and e2e."""
import subprocess
import sys
import re

try:
    import yaml
    import pytest
except ImportError:
    yaml = None
    pytest = None

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "defaults" / "main.yml"

EXPECTED = {
    "k8s_version": "v1.35.6",
    "cilium_version": "v1.19.5",
    "gateway_api_version": "v1.6.0",
    "cert_manager_version": "v1.21.0",
    "metallb_version": "v0.16.1",
    "hetzner_ccm_version": "v1.33.0",
    "hetzner_csi_version": "v2.22.0",
    "kubespray_version": "v2.31.0",
    "kubectl_version": "{{ k8s_version }}",
    "hcloud_cli_version": "v1.65.0",
    "yq_version": "v4.44.6",
    "keda_chart_version": "2.20.1",
    "es_version": "9.4.3",
    "kibana_version": "9.4.3",
    "apm_server_version": "9.4.3",
    "postal_version": "3.3.7",
    "blackbox_chart_version": "11.15.1",
    "dragonfly_operator_version": "v1.6.1",
    "dragonfly_image_version": "v1.39.0",
    "vm_operator_version": "0.66.2",
    "pmm_server_version": "3.8.1",
    "vault_version": "2.0.3",
    "vault_chart_version": "0.34.0",
    "caddy_image_tag": "2.11.4-alpine",
    "coroot_operator_chart_version": "0.9.7",
    "coroot_chart_version": "0.3.3",
    "coroot_image_tag": "1.23.3",
    "coroot_node_agent_image_tag": "1.34.2",
    "coroot_cluster_agent_image_tag": "1.7.1",
    "coroot_clickhouse_image_tag": "25.11.2-ubi9-0",
    "eso_chart_version": "2.7.0",
}

STALE = [
    "v1.35.4", "v1.19.4", "v1.5.1", "v1.20.2",
    "v0.15.3", "v1.31.0", "v2.21.0", "v1.42.0",
    "2.19.0", "9.4.1", "3.3.6",
    "11.10.0", "v1.5.0", "v1.38.1",
    "0.59.3", "2.5.0",
]


# ==================== UNIT TESTS ====================

class TestCentralDefaults:
    """Unit: defaults/main.yml has correct version values."""

    @pytest.fixture(scope="class")
    @classmethod
    def data(cls):
        return yaml.safe_load(DEFAULTS.read_text())

    @pytest.mark.parametrize("var,expected", EXPECTED.items())
    def test_version_matches(self, data, var, expected):
        assert data.get(var) is not None, f"Missing {var}"
        actual = str(data[var])
        if var == "kubectl_version":
            assert "k8s_version" in actual, "kubectl_version should reference k8s_version"
        else:
            assert actual == expected, f"{var} = {actual}, expected {expected}"

    def test_no_missing_version_vars(self, data):
        """All expected version variables exist."""
        for var in EXPECTED:
            assert var in data, f"Missing version variable: {var}"


class TestNoStaleVersions:
    """Unit: no stale hardcoded versions remain in role files."""

    @pytest.mark.parametrize("stale", STALE)
    def test_no_stale_in_roles(self, stale):
        roles = ROOT / "roles"
        for yml in roles.rglob("*.yml"):
            content = yml.read_text()
            assert stale not in content, f"Stale version {stale} found in {yml.relative_to(ROOT)}"


class TestNoLatestTags:
    """Unit: no :latest image tags in role files (excluding pod-security annotations)."""

    def test_no_latest_image_tags(self):
        for yml in (ROOT / "roles").rglob("*.yml"):
            for i, line in enumerate(yml.read_text().splitlines(), 1):
                s = line.strip()
                if "pod-security.kubernetes.io" in s:
                    continue
                assert not re.search(r'[\s"]:\s*latest\s*$|:latest[\s"}]', s), \
                    f":latest tag in {yml.relative_to(ROOT)}:{i}"

    def test_no_latest_download_urls(self):
        for yml in (ROOT / "roles").rglob("*.yml"):
            content = yml.read_text()
            clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', content)
            assert "releases/latest/" not in clean, f"/latest/ URL in {yml.relative_to(ROOT)}"


# ==================== COMPONENT TESTS ====================

class TestProfileConsistency:
    """Component: all profiles agree with central defaults on k8s version."""

    def test_all_profiles_k8s_version(self):
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        profiles_dir = ROOT / "platform-orchestrator" / "profiles"
        for yml in profiles_dir.glob("*.yaml"):
            content = yml.read_text()
            assert f"version: {exp}" in content, \
                f"{yml.relative_to(ROOT)} missing k8s {exp}"


class TestInventoryConsistency:
    """Component: inventory.example matches central defaults."""

    def test_inventory_k8s_version(self):
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        inv = (ROOT / "inventory.example").read_text()
        assert f"k8s_version: {exp}" in inv


class TestKubectlUsesVariable:
    """Component: kubectl uses k8s_version variable, not hardcoded."""

    def test_kubectl_uses_var(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${K8S_VER}" in ns, "kubectl should use ${K8S_VER} variable"
        cleaned = ns.replace("${K8S_VER}", "")
        assert "dl.k8s.io/release/v1.35" not in cleaned, \
            "Hardcoded k8s version still present after removing K8S_VER"


class TestCLIPinned:
    """Component: hcloud and yq CLI tools are pinned to specific versions."""

    def test_hcloud_pinned(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${HCLOUD_VER}" in ns, "hcloud should use ${HCLOUD_VER} variable"

    def test_yq_pinned(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${YQ_VER}" in ns, "yq should use ${YQ_VER} variable"

    def test_no_latest_urls_in_network_security(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', ns)
        assert "releases/latest/" not in clean, "Unpinned /latest/ URL found"


class TestRoleVersionsMatchCentral:
    """Component: role defaults match central defaults/main.yml."""

    @pytest.fixture(scope="class")
    @classmethod
    def central(cls):
        return yaml.safe_load(DEFAULTS.read_text())

    def test_blackbox(self, central):
        r = yaml.safe_load((ROOT / "roles/blackbox-exporter/defaults/main.yml").read_text())
        assert r.get("blackbox_chart_version") == central["blackbox_chart_version"]

    def test_apm(self, central):
        r = yaml.safe_load((ROOT / "roles/apm-server/defaults/main.yml").read_text())
        assert str(r.get("apm_version")) == central["apm_server_version"]

    def test_elasticsearch(self, central):
        r = yaml.safe_load((ROOT / "roles/elasticsearch/defaults/main.yml").read_text())
        assert r.get("es_version") == central["es_version"]
        assert r.get("kibana_version") == central["kibana_version"]

    def test_dragonfly(self, central):
        r = yaml.safe_load((ROOT / "roles/dragonfly/defaults/main.yml").read_text())
        assert r.get("dragonfly_operator_version") == central["dragonfly_operator_version"]
        assert r.get("dragonfly_image_version") == central["dragonfly_image_version"]

    def test_postal(self, central):
        r = yaml.safe_load((ROOT / "roles/postal/defaults/main.yml").read_text())
        assert r.get("postal_version") == central["postal_version"]

    def test_keda_in_tasks(self):
        content = (ROOT / "roles/k8s-autoscaling/tasks/main.yml").read_text()
        assert "2.20.1" in content, "keda 2.20.1 should be in autoscaling tasks"

    def test_vm_operator_in_tasks(self):
        content = (ROOT / "roles/k8s-observability/tasks/main.yml").read_text()
        assert "0.66.2" in content, "vm_operator 0.66.2 should be in observability tasks"

    def test_eso_in_tasks(self):
        content = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()
        assert "2.7.0" in content, "eso 2.7.0 should be in secrets tasks"

    def test_cluster_mgmt_uses_variables(self):
        content = (ROOT / "roles/k8s-cluster-management/tasks/main.yml").read_text()
        assert "cert_manager_version | default" in content, "Should use cert_manager_version variable"
        assert "metallb_version | default" in content, "Should use metallb_version variable"
        assert "hetzner_ccm_version | default" in content, "Should use hetzner_ccm_version variable"
        assert "hetzner_csi_version | default" in content, "Should use hetzner_csi_version variable"


class TestESVersionCompatibility:
    """Component: ES, Kibana, and APM must be the same version."""

    def test_es_kibana_apm_same_version(self):
        d = yaml.safe_load(DEFAULTS.read_text())
        assert d["es_version"] == d["kibana_version"] == d["apm_server_version"], \
            "ES, Kibana, and APM must be the same version for compatibility"


class TestAllK8sVersionsMatch:
    """Component: every profile and inventory example uses the same k8s version."""

    def test_all_k8s_versions_match(self):
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        profiles_dir = ROOT / "platform-orchestrator" / "profiles"
        for yml in profiles_dir.glob("*.yaml"):
            assert f"version: {exp}" in yml.read_text(), \
                f"{yml.relative_to(ROOT)} has wrong k8s version"
        inv = (ROOT / "inventory.example").read_text()
        assert f"k8s_version: {exp}" in inv, "inventory.example has wrong k8s version"


# ==================== E2E TESTS ====================

class TestVersionMatrixScript:
    """E2E: verify-version-matrix.py script passes on current state."""

    def test_verify_script_passes(self):
        script = ROOT / "scripts" / "verify-version-matrix.py"
        assert script.exists(), "verify-version-matrix.py not found"
        r = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, timeout=30,
            cwd=str(ROOT),
        )
        assert r.returncode == 0, f"verify-version-matrix.py failed:\n{r.stdout}\n{r.stderr}"
