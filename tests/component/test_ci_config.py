"""Component tests: CI config files work together correctly."""

import os
import json
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def read(path):
    with open(path) as f:
        return f.read()


class TestRenovateMarkersInDefaults:
    """Component: renovate markers in defaults/main.yml match regex managers."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.defaults = read(os.path.join(REPO_ROOT, "defaults", "main.yml"))
        with open(os.path.join(REPO_ROOT, ".renovaterc.json")) as f:
            self.renovate = json.load(f)

    @pytest.mark.component
    def test_k8s_version_has_renovate_marker(self):
        assert "# renovate: datasource=github-releases depName=kubernetes/kubernetes" in self.defaults

    @pytest.mark.component
    def test_cilium_version_has_renovate_marker(self):
        assert "# renovate: datasource=github-releases depName=cilium/cilium" in self.defaults

    @pytest.mark.component
    def test_gitlab_chart_has_helm_marker(self):
        assert "depName=gitlab" in self.defaults

    @pytest.mark.component
    def test_argocd_chart_has_helm_marker(self):
        assert "depName=argo-cd" in self.defaults

    @pytest.mark.component
    def test_es_version_has_docker_marker(self):
        assert "depName=elasticsearch" in self.defaults

    @pytest.mark.component
    def test_postal_has_renovate_marker(self):
        assert "depName=postalserver/postal" in self.defaults

    @pytest.mark.component
    def test_renovate_has_helm_manager(self):
        datasources = [m.get("datasourceTemplate") for m in self.renovate.get("regexManagers", [])]
        assert "helm" in datasources

    @pytest.mark.component
    def test_renovate_has_docker_manager(self):
        datasources = [m.get("datasourceTemplate") for m in self.renovate.get("regexManagers", [])]
        assert "docker" in datasources


class TestCIWorkflowReferencesTestDirectory:
    """Component: CI workflow correctly references the tests/ directory."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.ci = read(os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml"))

    @pytest.mark.component
    def test_ci_runs_pytest(self):
        assert "pytest" in self.ci

    @pytest.mark.component
    def test_ci_references_tests_directory(self):
        assert "tests/" in self.ci

    @pytest.mark.component
    def test_ci_installs_requirements_txt(self):
        assert "requirements.txt" in self.ci or "pip install" in self.ci

    @pytest.mark.component
    def test_ci_runs_version_matrix_test(self):
        assert "test_version_matrix.py" in self.ci


class TestPreCommitConsistency:
    """Component: pre-commit hooks reference actual config files."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.precommit = read(os.path.join(REPO_ROOT, ".pre-commit-config.yaml"))

    @pytest.mark.component
    def test_precommit_references_yamllint_config(self):
        assert ".yamllint.yaml" in self.precommit

    @pytest.mark.component
    def test_precommit_references_ansible_lint_config(self):
        assert ".ansible-lint.yml" in self.precommit

    @pytest.mark.component
    def test_precommit_has_shellcheck(self):
        assert "shellcheck" in self.precommit

    @pytest.mark.component
    def test_precommit_has_gitleaks(self):
        assert "gitleaks" in self.precommit


class TestTrivyWorkflowIntegration:
    """Component: Trivy workflow integrates with GitHub Security tab."""

    @pytest.fixture(autouse=True)
    def _load(self):
        self.trivy = read(os.path.join(REPO_ROOT, ".github", "workflows", "trivy.yml"))

    @pytest.mark.component
    def test_trivy_uploads_sarif(self):
        assert "upload-sarif" in self.trivy

    @pytest.mark.component
    def test_trivy_has_sarif_output(self):
        assert "sarif" in self.trivy

    @pytest.mark.component
    def test_trivy_has_severity_setting(self):
        assert "CRITICAL,HIGH" in self.trivy

    @pytest.mark.component
    def test_trivy_runs_on_push_to_main(self):
        assert "push:" in self.trivy
        assert "main" in self.trivy

    @pytest.mark.component
    def test_trivy_has_scheduled_run(self):
        assert "schedule:" in self.trivy
        assert "cron:" in self.trivy
