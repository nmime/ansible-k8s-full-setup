"""Unit tests: version matrix validation logic."""

import os
import re
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")

VALID_TIERS = ["minimal", "small", "medium", "production"]


def read(path):
    with open(path) as f:
        return f.read()


def parse_version_vars(content):
    """Parse all version-like variables from defaults/main.yml."""
    versions = {}
    for line in content.splitlines():
        m = re.match(r"^\s*(\w+version\w*)\s*:\s*['\"]?([^'\"#\n{]+?)['\"]?\s*$", line)
        if m:
            versions[m.group(1)] = m.group(2).strip()
    return versions


class TestVersionConsistency:
    """Unit: Elasticsearch and Kibana versions should match."""

    @pytest.fixture(autouse=True)
    def _versions(self):
        self.versions = parse_version_vars(read(DEFAULTS_PATH))

    @pytest.mark.unit
    def test_es_kibana_version_match(self):
        es = self.versions.get("es_version", "")
        kib = self.versions.get("kibana_version", "")
        assert es == kib, f"Elasticsearch ({es}) and Kibana ({kib}) versions should match"


class TestVersionFormatSemver:
    """Unit: all version values follow semver-compatible format."""

    @pytest.fixture(autouse=True)
    def _versions(self):
        self.versions = parse_version_vars(read(DEFAULTS_PATH))

    @pytest.mark.unit
    def test_chart_versions_are_semver(self):
        chart_vars = {k: v for k, v in self.versions.items() if "chart" in k.lower()}
        semver_re = re.compile(r"^\d+\.\d+\.\d+$")
        for var, val in chart_vars.items():
            assert semver_re.match(val), f"{var} = '{val}' is not valid semver (x.y.z)"

    @pytest.mark.unit
    def test_v_prefixed_versions_have_v_prefix(self):
        v_vars = {
            "k8s_version",
            "cilium_version",
            "gateway_api_version",
            "argocd_version",
        }
        for var in v_vars:
            val = self.versions.get(var, "")
            assert val.startswith("v"), f"{var} should start with 'v': {val}"

    @pytest.mark.unit
    def test_no_zero_versions(self):
        for var, val in self.versions.items():
            clean = val.lstrip("v")
            parts = clean.split(".")
            # Allow 0.x for chart versions and daytona (alpha software)
            if parts[0] == "0":
                if "chart" in var or var in ("daytona_chart_version", "metallb_version"):
                    continue
                pytest.fail(f"{var} major version is 0: {val}")


class TestTierValidity:
    """Unit: tier references in defaults are consistent."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(DEFAULTS_PATH)

    @pytest.mark.unit
    def test_default_tier_is_valid(self):
        m = re.search(r"^tier:\s*(\w+)", self.content, re.MULTILINE)
        assert m, "No tier default found"
        assert m.group(1) in VALID_TIERS, f"Invalid default tier: {m.group(1)}"

    @pytest.mark.unit
    def test_all_tier_references_valid(self):
        """Check that tier Jinja2 references only mention valid tiers."""
        tier_refs = re.findall(r"tier\s+in\s+\[([^\]]+)\]", self.content)
        for ref in tier_refs:
            tiers_in_ref = re.findall(r"'(\w+)'", ref)
            for t in tiers_in_ref:
                assert t in VALID_TIERS, f"Unknown tier '{t}' found in Jinja2 expression"

    @pytest.mark.unit
    def test_all_tier_equals_references_valid(self):
        tier_eqs = re.findall(r"tier\s*==\s*'(\w+)'", self.content)
        for t in tier_eqs:
            assert t in VALID_TIERS, f"Unknown tier '{t}' in tier comparison"
