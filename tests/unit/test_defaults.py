"""
Unit tests: validate version parsing, YAML structure, and requirements.txt format.
These are fast, isolated tests with no external dependencies.
"""

import re
import os
import json
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULTS_PATH = os.path.join(REPO_ROOT, "defaults", "main.yml")
REQUIREMENTS_TXT = os.path.join(REPO_ROOT, "requirements.txt")
REQUIREMENTS_YML = os.path.join(REPO_ROOT, "requirements.yml")
RENOVATE_PATH = os.path.join(REPO_ROOT, ".renovaterc.json")
CI_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
TRIVY_WORKFLOW_PATH = os.path.join(REPO_ROOT, ".github", "workflows", "trivy.yml")
PRECOMMIT_PATH = os.path.join(REPO_ROOT, ".pre-commit-config.yaml")
YAMLLINT_PATH = os.path.join(REPO_ROOT, ".yamllint.yaml")


# ── helpers ──────────────────────────────────────────────────────

def read(path):
    with open(path) as f:
        return f.read()


def parse_version(line):
    """Extract 'name: value' or 'name: "value"' from a YAML-like line."""
    m = re.match(r"^\s*(\w+)\s*:\s*['\"]?([^'\"#\n]+?)['\"]?\s*$", line)
    if m:
        return m.group(1), m.group(2).strip()
    return None, None


def parse_versions(content):
    """Return a dict of version variable name -> value from defaults/main.yml."""
    versions = {}
    for line in content.splitlines():
        key, val = parse_version(line)
        if key and val and not val.startswith("{{") and not val.startswith("{"):
            versions[key] = val
    return versions


# ── defaults/main.yml structure tests ────────────────────────────

class TestDefaultsFileExists:
    @pytest.mark.unit
    def test_defaults_main_yml_exists(self):
        assert os.path.isfile(DEFAULTS_PATH), f"Missing {DEFAULTS_PATH}"

    @pytest.mark.unit
    def test_defaults_not_empty(self):
        content = read(DEFAULTS_PATH)
        assert len(content) > 100, "defaults/main.yml appears empty"


class TestDefaultsVersions:
    """Unit: every version variable has a proper semver / release value."""

    @pytest.fixture(autouse=True)
    def _versions(self):
        self.versions = parse_versions(read(DEFAULTS_PATH))

    @pytest.mark.unit
    def test_k8s_version_present(self):
        assert "k8s_version" in self.versions
        assert re.match(r"^v\d+\.\d+\.\d+$", self.versions["k8s_version"])

    @pytest.mark.unit
    def test_cilium_version_present(self):
        assert "cilium_version" in self.versions
        assert re.match(r"^v\d+\.\d+\.\d+$", self.versions["cilium_version"])

    @pytest.mark.unit
    def test_gateway_api_version_present(self):
        assert "gateway_api_version" in self.versions
        assert re.match(r"^v\d+\.\d+\.\d+$", self.versions["gateway_api_version"])

    @pytest.mark.unit
    def test_gitlab_chart_version_present(self):
        assert "gitlab_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["gitlab_chart_version"])

    @pytest.mark.unit
    def test_argocd_chart_version_present(self):
        assert "argocd_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["argocd_chart_version"])

    @pytest.mark.unit
    def test_postgresql_version_is_integer(self):
        assert "postgresql_version" in self.versions
        assert self.versions["postgresql_version"].isdigit()

    @pytest.mark.unit
    def test_es_version_present(self):
        assert "es_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["es_version"])

    @pytest.mark.unit
    def test_kibana_version_present(self):
        assert "kibana_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["kibana_version"])

    @pytest.mark.unit
    def test_postal_version_present(self):
        assert "postal_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["postal_version"])

    @pytest.mark.unit
    def test_keda_chart_version_present(self):
        assert "keda_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["keda_chart_version"])

    @pytest.mark.unit
    def test_temporal_chart_version_present(self):
        assert "temporal_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["temporal_chart_version"])

    @pytest.mark.unit
    def test_object_storage_chart_version_present(self):
        assert "object_storage_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["object_storage_chart_version"])

    @pytest.mark.unit
    def test_daytona_chart_version_present(self):
        assert "daytona_chart_version" in self.versions
        assert re.match(r"^\d+\.\d+\.\d+$", self.versions["daytona_chart_version"])

    @pytest.mark.unit
    def test_argocd_version_present(self):
        assert "argocd_version" in self.versions
        assert re.match(r"^v\d+\.\d+\.\d+$", self.versions["argocd_version"])


class TestDefaultsTierDefinitions:
    """Unit: tier definitions are complete and consistent."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(DEFAULTS_PATH)

    @pytest.mark.unit
    def test_all_tiers_defined(self):
        for tier in ["minimal", "small", "medium", "production"]:
            assert f"{tier}:" in self.content, f"Tier '{tier}' not defined"

    @pytest.mark.unit
    def test_minimal_nodes(self):
        assert "nodes: 2" in self.content or "nodes: 2\n" in self.content

    @pytest.mark.unit
    def test_medium_ha_true(self):
        lines = self.content.splitlines()
        in_medium = False
        for line in lines:
            if line.startswith("medium:"):
                in_medium = True
            elif in_medium and line and not line.startswith(" "):
                break
            elif in_medium and "ha: true" in line:
                return
        pytest.fail("medium tier missing ha: true")


class TestRequirementsTxt:
    """Unit: requirements.txt is well-formed with pinned versions."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(REQUIREMENTS_TXT)
        self.lines = [l.strip() for l in self.content.splitlines() if l.strip() and not l.strip().startswith("#")]

    @pytest.mark.unit
    def test_requirements_txt_exists(self):
        assert os.path.isfile(REQUIREMENTS_TXT)

    @pytest.mark.unit
    def test_all_lines_pinned_with_double_equals(self):
        for line in self.lines:
            if "==" not in line:
                pytest.fail(f"Package not pinned with '==': {line}")

    @pytest.mark.unit
    def test_ansible_core_present(self):
        packages = [l.split("==")[0] for l in self.lines]
        assert "ansible-core" in packages

    @pytest.mark.unit
    def test_pytest_present(self):
        packages = [l.split("==")[0] for l in self.lines]
        assert "pytest" in packages

    @pytest.mark.unit
    def test_yamllint_present(self):
        packages = [l.split("==")[0] for l in self.lines]
        assert "yamllint" in packages

    @pytest.mark.unit
    def test_ansible_lint_present(self):
        packages = [l.split("==")[0] for l in self.lines]
        assert "ansible-lint" in packages

    @pytest.mark.unit
    def test_no_wildcard_versions(self):
        for line in self.lines:
            assert "*" not in line, f"Wildcard version found: {line}"

    @pytest.mark.unit
    def test_no_comparison_operators(self):
        for line in self.lines:
            if "==" not in line:
                continue
            rest = line.split("==")[1]
            for op in [">", "<", "~", "!"]:
                assert op not in rest, f"Comparison operator in pinned version: {line}"


class TestRenovateConfig:
    """Unit: Renovate config is valid JSON with expected structure."""

    @pytest.fixture(autouse=True)
    def _config(self):
        with open(RENOVATE_PATH) as f:
            self.config = json.load(f)

    @pytest.mark.unit
    def test_renovate_config_exists(self):
        assert os.path.isfile(RENOVATE_PATH)

    @pytest.mark.unit
    def test_regex_managers_present(self):
        assert "regexManagers" in self.config
        assert len(self.config["regexManagers"]) > 0

    @pytest.mark.unit
    def test_helm_regex_manager(self):
        datasources = [m.get("datasourceTemplate", "") for m in self.config["regexManagers"]]
        assert "helm" in datasources, "No Helm regex manager found"

    @pytest.mark.unit
    def test_docker_regex_manager(self):
        datasources = [m.get("datasourceTemplate", "") for m in self.config["regexManagers"]]
        assert "docker" in datasources, "No Docker regex manager found"

    @pytest.mark.unit
    def test_github_releases_regex_manager(self):
        datasources = [m.get("datasourceTemplate", "") for m in self.config["regexManagers"]]
        assert "github-releases" in datasources

    @pytest.mark.unit
    def test_package_rules_present(self):
        assert "packageRules" in self.config
        assert len(self.config["packageRules"]) > 0

    @pytest.mark.unit
    def test_enabled_managers(self):
        managers = self.config.get("enabledManagers", [])
        assert "github-actions" in managers
        assert "custom.regex" in managers


class TestCIWorkflowFile:
    """Unit: CI workflow YAML file exists and has expected structure."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(CI_WORKFLOW_PATH)

    @pytest.mark.unit
    def test_ci_workflow_exists(self):
        assert os.path.isfile(CI_WORKFLOW_PATH)

    @pytest.mark.unit
    def test_has_yaml_lint_job(self):
        assert "lint-yaml:" in self.content or "lint-yaml " in self.content

    @pytest.mark.unit
    def test_has_ansible_lint_job(self):
        assert "lint-ansible:" in self.content or "lint-ansible " in self.content

    @pytest.mark.unit
    def test_has_syntax_check_job(self):
        assert "ansible-syntax:" in self.content or "ansible-syntax " in self.content

    @pytest.mark.unit
    def test_has_shellcheck_job(self):
        assert "shellcheck:" in self.content or "shellcheck " in self.content

    @pytest.mark.unit
    def test_has_python_tests_job(self):
        assert "python-tests:" in self.content or "python-tests " in self.content

    @pytest.mark.unit
    def test_has_version_matrix_job(self):
        assert "version-matrix:" in self.content or "version-matrix " in self.content

    @pytest.mark.unit
    def test_has_trivy_job(self):
        assert "trivy" in self.content.lower()

    @pytest.mark.unit
    def test_triggers_on_push(self):
        assert "push:" in self.content

    @pytest.mark.unit
    def test_triggers_on_pull_request(self):
        assert "pull_request:" in self.content


class TestTrivyWorkflowFile:
    """Unit: Trivy workflow YAML file exists and has expected structure."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(TRIVY_WORKFLOW_PATH)

    @pytest.mark.unit
    def test_trivy_workflow_exists(self):
        assert os.path.isfile(TRIVY_WORKFLOW_PATH)

    @pytest.mark.unit
    def test_has_schedule_trigger(self):
        assert "schedule:" in self.content

    @pytest.mark.unit
    def test_has_fs_scan(self):
        assert "scan-type: fs" in self.content

    @pytest.mark.unit
    def test_has_config_scan(self):
        assert "scan-type: config" in self.content


class TestPreCommitConfig:
    """Unit: pre-commit config exists and has expected hooks."""

    @pytest.fixture(autouse=True)
    def _content(self):
        self.content = read(PRECOMMIT_PATH)

    @pytest.mark.unit
    def test_precommit_config_exists(self):
        assert os.path.isfile(PRECOMMIT_PATH)

    @pytest.mark.unit
    def test_has_yamllint_hook(self):
        assert "yamllint" in self.content

    @pytest.mark.unit
    def test_has_shellcheck_hook(self):
        assert "shellcheck" in self.content

    @pytest.mark.unit
    def test_has_ansible_lint_hook(self):
        assert "ansible-lint" in self.content

    @pytest.mark.unit
    def test_has_pre_commit_hooks(self):
        assert "pre-commit-hooks" in self.content

    @pytest.mark.unit
    def test_has_gitleaks_hook(self):
        assert "gitleaks" in self.content


class TestShellScripts:
    """Unit: shell scripts have proper shebangs and are well-formed."""

    SHELL_SCRIPTS = [
        "run_all.sh",
        "run_tier.sh",
        "teardown.sh",
        "scripts/cycle-test.sh",
    ]

    @pytest.mark.unit
    def test_shell_scripts_exist(self):
        for script in self.SHELL_SCRIPTS:
            path = os.path.join(REPO_ROOT, script)
            assert os.path.isfile(path), f"Missing script: {script}"

    @pytest.mark.unit
    def test_shell_scripts_have_shebang(self):
        for script in self.SHELL_SCRIPTS:
            path = os.path.join(REPO_ROOT, script)
            with open(path) as f:
                first_line = f.readline()
            assert first_line.startswith("#!"), f"{script} missing shebang"
            assert "bash" in first_line or "sh" in first_line

    @pytest.mark.unit
    def test_no_tabs_in_shell_scripts(self):
        """Tabs can cause issues in some CI environments."""
        for script in self.SHELL_SCRIPTS:
            path = os.path.join(REPO_ROOT, script)
            content = read(path)
            # Allow tabs only inside heredocs (rare edge case)
            lines_with_tabs = [l for l in content.splitlines() if "\t" in l and not l.strip().startswith("#")]
            assert len(lines_with_tabs) == 0, f"{script} has tab characters in non-comment lines"


class TestYamllintConfig:
    """Unit: .yamllint.yaml exists and is valid."""

    @pytest.mark.unit
    def test_yamllint_config_exists(self):
        assert os.path.isfile(YAMLLINT_PATH)

    @pytest.mark.unit
    def test_yamllint_has_rules(self):
        content = read(YAMLLINT_PATH)
        assert "rules:" in content

    @pytest.mark.unit
    def test_yamllint_extends_default(self):
        content = read(YAMLLINT_PATH)
        assert "extends: default" in content
