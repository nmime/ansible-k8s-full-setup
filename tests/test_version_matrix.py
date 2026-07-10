#!/usr/bin/env python3
"""Tests for version baseline upgrade: unit, component, and e2e."""
import subprocess, sys, re, yaml, pytest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = ROOT / "defaults" / "main.yml"

EXPECTED = {
    "k8s_version": "v1.35.6", "cilium_version": "v1.19.5",
    "gateway_api_version": "v1.6.0", "cert_manager_version": "v1.21.0",
    "metallb_version": "v0.16.1", "hetzner_ccm_version": "v1.33.0",
    "hetzner_csi_version": "v2.22.0", "kubespray_version": "v2.31.0",
    "keda_chart_version": "2.20.1", "es_version": "9.4.3",
    "kibana_version": "9.4.3", "apm_server_version": "9.4.3",
    "postal_version": "3.3.7", "blackbox_chart_version": "11.15.1",
    "dragonfly_operator_version": "v1.6.1", "dragonfly_image_version": "v1.39.0",
    "vm_operator_version": "0.66.2", "eso_chart_version": "2.7.0",
    "hcloud_cli_version": "v1.42.0", "yq_version": "v4.44.6",
}

STALE = ["v1.35.4", "v1.19.4", "v1.5.1", "v1.20.2", "v0.15.3",
         "v1.31.0", "v2.21.0", "2.19.0", "9.4.1", "3.3.6",
         "11.10.0", "v1.5.0", "v1.38.1", "0.59.3", "2.5.0"]


# ==================== UNIT TESTS ====================
class TestCentralDefaults:
    """Unit: defaults/main.yml has correct version values."""
    @pytest.fixture(scope="class")
    def data(self):
        return yaml.safe_load(DEFAULTS.read_text())

    @pytest.mark.parametrize("var,expected", EXPECTED.items())
    def test_version_matches(self, data, var, expected):
        assert data.get(var) is not None, f"Missing {var}"
        assert str(data[var]) == expected, f"{var} = {data[var]}, expected {expected}"

    def test_kubectl_refs_k8s_version(self, data):
        kv = str(data.get("kubectl_version", ""))
        assert "k8s_version" in kv, "kubectl_version should reference k8s_version"


class TestNoStaleVersions:
    """Unit: no stale hardcoded versions remain in role files."""
    @pytest.mark.parametrize("stale", STALE)
    def test_no_stale_in_roles(self, stale):
        roles = ROOT / "roles"
        for yml in roles.rglob("*.yml"):
            assert stale not in yml.read_text(), f"Stale {stale} in {yml.relative_to(ROOT)}"


class TestNoLatestTags:
    """Unit: no :latest image tags in role files."""
    def test_no_latest_image_tags(self):
        for yml in (ROOT / "roles").rglob("*.yml"):
            for i, line in enumerate(yml.read_text().splitlines(), 1):
                s = line.strip()
                if "pod-security.kubernetes.io" in s:
                    continue
                assert not re.search(r'["\s:]latest\s*$|["\s:]latest["\s}]', s), \
                    f":latest tag in {yml.relative_to(ROOT)}:{i}"

    def test_no_latest_download_urls(self):
        for yml in (ROOT / "roles").rglob("*.yml"):
            clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', yml.read_text())
            assert "releases/latest/" not in clean, f"/latest/ URL in {yml.relative_to(ROOT)}"


# ==================== COMPONENT TESTS ====================
class TestProfileConsistency:
    """Component: all profiles agree with central defaults on k8s version."""
    def test_all_profiles_k8s_version(self):
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        for yml in (ROOT / "platform-orchestrator" / "profiles").glob("*.yaml"):
            assert f"version: {exp}" in yml.read_text(), \
                f"{yml.relative_to(ROOT)} missing k8s {exp}"


class TestInventoryConsistency:
    """Component: inventory.example matches central defaults."""
    def test_inventory_k8s_version(self):
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        inv = (ROOT / "inventory.example").read_text()
        assert f"k8s_version: {exp}" in inv


class TestKubectlUsesVariable:
    """Component: kubectl uses k8s_version, not hardcoded."""
    def test_kubectl_uses_var(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${K8S_VER}" in ns
        cleaned = ns.replace('${K8S_VER}', '')
        assert "dl.k8s.io/release/v1.35" not in cleaned


class TestCLIPinned:
    """Component: hcloud and yq are pinned."""
    def test_hcloud_pinned(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${HCLOUD_VER}" in ns

    def test_yq_pinned(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        assert "${YQ_VER}" in ns

    def test_no_latest_urls(self):
        ns = (ROOT / "roles" / "network-security" / "tasks" / "main.yml").read_text()
        clean = re.sub(r'pod-security\.kubernetes\.io[^\n]*', '', ns)
        assert "releases/latest/" not in clean


class TestRoleVersionsMatchCentral:
    """Component: role defaults match central defaults/main.yml."""
    @pytest.fixture(scope="class")
    def central(self):
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

    def test_keda(self):
        content = (ROOT / "roles/k8s-autoscaling/tasks/main.yml").read_text()
        assert '"2.20.1"' in content

    def test_vm_operator(self):
        content = (ROOT / "roles/k8s-observability/tasks/main.yml").read_text()
        assert '"0.66.2"' in content

    def test_eso(self):
        content = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()
        assert '"2.7.0"' in content


# ==================== E2E TESTS ====================
class TestE2EVersionMatrix:
    """E2E: verify-version-matrix.py passes end-to-end."""
    def test_verify_script_passes(self):
        script = ROOT / "scripts" / "verify-version-matrix.py"
        assert script.exists()
        r = subprocess.run([sys.executable, str(script)],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, f"verify-version-matrix.py failed:\n{r.stdout}\n{r.stderr}"

    def test_elastic_trio_match(self):
        """ES, Kibana, APM must all share the same version."""
        d = yaml.safe_load(DEFAULTS.read_text())
        assert d["es_version"] == d["kibana_version"] == d["apm_server_version"]

    def test_all_k8s_versions_match(self):
        """Every profile and inventory example uses the same k8s version."""
        exp = yaml.safe_load(DEFAULTS.read_text())["k8s_version"]
        for yml in (ROOT / "platform-orchestrator" / "profiles").glob("*.yaml"):
            assert f"version: {exp}" in yml.read_text()
        assert f"k8s_version: {exp}" in (ROOT / "inventory.example").read_text()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
