"""
Tests for scripts/validate-local.sh — the local validation script that replaces
GitHub Actions CI.

Unit tests: script structure, syntax, argument handling, and output contracts.
Component tests: integration with repo config files (.yamllint.yaml, etc.).
E2E tests: end-to-end execution in a controlled environment.
"""

import os
import stat
import subprocess
import tempfile
import textwrap

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(REPO_ROOT, "scripts", "validate-local.sh")


# ──────────────────────────────────────────────────────────────────────────────
# Unit Tests
# ──────────────────────────────────────────────────────────────────────────────


class TestScriptExists:
    """Unit: script file exists, is readable, and is executable."""

    def test_validate_local_script_exists(self):
        assert os.path.isfile(SCRIPT_PATH), "scripts/validate-local.sh should exist"

    def test_validate_local_script_is_readable(self):
        assert os.access(SCRIPT_PATH, os.R_OK), "Script should be readable"

    def test_validate_local_script_is_executable(self):
        mode = os.stat(SCRIPT_PATH).st_mode
        assert mode & stat.S_IXUSR, "Script should have user execute permission"

    def test_validate_local_has_shebang(self):
        with open(SCRIPT_PATH, "r") as f:
            first_line = f.readline().strip()
        assert first_line.startswith("#!"), f"Expected shebang, got: {first_line}"

    def test_validate_local_uses_env_bash(self):
        with open(SCRIPT_PATH, "r") as f:
            first_line = f.readline().strip()
        assert "/usr/bin/env bash" in first_line, "Should use portable shebang: #!/usr/bin/env bash"


class TestScriptSyntax:
    """Unit: bash syntax check via shellcheck or bash -n."""

    def test_bash_syntax_is_valid(self):
        result = subprocess.run(
            ["bash", "-n", SCRIPT_PATH],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"Bash syntax error: {result.stderr}"

    def test_script_has_no_tabs(self):
        """Ensure script uses spaces, not tabs (shell best practice)."""
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        lines_with_tabs = [
            i + 1 for i, line in enumerate(content.splitlines())
            if "\t" in line
        ]
        assert not lines_with_tabs, f"Tabs found on lines: {lines_with_tabs}"


class TestScriptArguments:
    """Unit: argument parsing --help and unknown args."""

    def test_help_flag_shows_usage(self):
        result = subprocess.run(
            ["bash", SCRIPT_PATH, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, f"--help exited with {result.returncode}: {result.stderr}"
        assert "Usage:" in result.stdout, "Help output should contain 'Usage:'"
        assert "validate-local.sh" in result.stdout, "Help should mention the script name"

    def test_help_mentions_fail_fast(self):
        result = subprocess.run(
            ["bash", SCRIPT_PATH, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "--fail-fast" in result.stdout, "Help should document --fail-fast"

    def test_unknown_argument_exits_nonzero(self):
        result = subprocess.run(
            ["bash", SCRIPT_PATH, "--bogus-flag"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode != 0, "Unknown argument should cause non-zero exit"

    def test_help_mentions_checks(self):
        """Help output should list the key checks available."""
        result = subprocess.run(
            ["bash", SCRIPT_PATH, "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        output = result.stdout.lower()
        assert "yamllint" in output, "Help should mention yamllint"
        assert "pre-commit" in output, "Help should mention pre-commit"
        assert "shellcheck" in output, "Help should mention shellcheck"


class TestScriptStructure:
    """Unit: script contains expected structural elements."""

    @pytest.fixture(autouse=True)
    def _load_script(self):
        with open(SCRIPT_PATH, "r") as f:
            self.content = f.read()

    def test_has_strict_mode(self):
        assert "set -uo pipefail" in self.content, "Should use strict mode"

    def test_has_fail_fast_flag(self):
        assert "--fail-fast" in self.content, "Script should support --fail-fast"

    def test_has_summary_output(self):
        assert "Summary:" in self.content, "Script should print a summary"

    def test_checks_yamllint(self):
        assert "yamllint" in self.content, "Script should check yamllint"

    def test_checks_pre_commit(self):
        assert "pre-commit" in self.content, "Script should check pre-commit"

    def test_checks_ansible_lint(self):
        assert "ansible-lint" in self.content, "Script should check ansible-lint"

    def test_checks_shellcheck(self):
        assert "shellcheck" in self.content, "Script should check shellcheck"

    def test_checks_version_matrix(self):
        assert "verify-version-matrix" in self.content, "Script should check version-matrix"

    def test_checks_pytest(self):
        assert "pytest" in self.content, "Script should check pytest"

    def test_skips_missing_tools(self):
        """Script should handle missing tools gracefully."""
        assert "not found" in self.content.lower(), "Script should print 'not found' for missing tools"

    def test_has_colour_support(self):
        """Script should define colour codes (with tty check)."""
        assert "RED=" in self.content, "Script should define RED colour"
        assert "GREEN=" in self.content, "Script should define GREEN colour"

    def test_has_tty_check(self):
        """Colours should be disabled when stdout is not a tty."""
        assert "-t 1" in self.content or "isatty" in self.content, \
            "Script should check for tty before using colours"


# ──────────────────────────────────────────────────────────────────────────────
# Component Tests — script interacts with repo config files
# ──────────────────────────────────────────────────────────────────────────────


class TestScriptConfigIntegration:
    """Component: script references the actual config files present in the repo."""

    def test_yamllint_config_referenced(self):
        """Script should use .yamllint.yaml if it exists."""
        assert os.path.isfile(os.path.join(REPO_ROOT, ".yamllint.yaml")), \
            ".yamllint.yaml should exist"
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        assert ".yamllint.yaml" in content, "Script should reference .yamllint.yaml"

    def test_pre_commit_config_referenced(self):
        """Script should use .pre-commit-config.yaml if it exists."""
        assert os.path.isfile(os.path.join(REPO_ROOT, ".pre-commit-config.yaml")), \
            ".pre-commit-config.yaml should exist"
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        assert ".pre-commit-config.yaml" in content, "Script should reference .pre-commit-config.yaml"

    def test_ansible_lint_config_referenced(self):
        """Script should use .ansible-lint.yml if it exists."""
        assert os.path.isfile(os.path.join(REPO_ROOT, ".ansible-lint.yml")), \
            ".ansible-lint.yml should exist"
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        assert ".ansible-lint.yml" in content, "Script should reference .ansible-lint.yml"

    def test_version_matrix_script_referenced(self):
        """Script should call verify-version-matrix.py."""
        assert os.path.isfile(os.path.join(REPO_ROOT, "scripts", "verify-version-matrix.py")), \
            "scripts/verify-version-matrix.py should exist"
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        assert "verify-version-matrix.py" in content, "Script should reference verify-version-matrix.py"

    def test_tests_directory_referenced(self):
        """Script should run tests from the tests/ directory."""
        assert os.path.isdir(os.path.join(REPO_ROOT, "tests")), \
            "tests/ directory should exist"
        with open(SCRIPT_PATH, "r") as f:
            content = f.read()
        assert "tests/" in content, "Script should reference tests/ directory"


# ──────────────────────────────────────────────────────────────────────────────
# E2E Tests — run the script in a controlled environment
# ──────────────────────────────────────────────────────────────────────────────


class TestValidateLocalE2E:
    """E2E: run validate-local.sh against a minimal mock repo."""

    @pytest.fixture
    def mock_repo(self, tmp_path):
        """Create a minimal mock repository structure."""
        scripts = tmp_path / "scripts"
        scripts.mkdir()

        # Copy the real script content into the mock repo
        validate_script = scripts / "validate-local.sh"
        with open(SCRIPT_PATH, "r") as f:
            validate_script.write_text(f.read())
        validate_script.chmod(0o755)

        # Minimal config files
        (tmp_path / ".yamllint.yaml").write_text("extends: default\n")
        (tmp_path / ".pre-commit-config.yaml").write_text(
            "repos:\n  - repo: local\n    hooks: []\n"
        )

        # A valid YAML file
        (tmp_path / "test.yml").write_text("---\nkey: value\n")

        # Initialize a git repo so pre-commit doesn't crash
        subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(tmp_path), capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(tmp_path), capture_output=True)

        yield tmp_path

    def test_script_runs_in_mock_repo(self, mock_repo):
        """Script should complete without crashing even when tools are missing."""
        result = subprocess.run(
            ["bash", str(mock_repo / "scripts" / "validate-local.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(mock_repo),
        )
        # Script should not crash (exit 0 or 1 is acceptable; 127+ is a crash)
        assert result.returncode in (0, 1), \
            f"Script crashed with exit {result.returncode}: {result.stderr}"

    def test_script_prints_summary(self, mock_repo):
        """Script should always print a summary line."""
        result = subprocess.run(
            ["bash", str(mock_repo / "scripts" / "validate-local.sh")],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(mock_repo),
        )
        output = result.stdout + result.stderr
        assert "Summary:" in output or "passed" in output.lower(), \
            "Script should print a summary of results"

    def test_help_runs_in_mock_repo(self, mock_repo):
        """--help should work regardless of repo state."""
        result = subprocess.run(
            ["bash", str(mock_repo / "scripts" / "validate-local.sh"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(mock_repo),
        )
        assert result.returncode == 0, f"--help failed: {result.stderr}"

    def test_fail_fast_exists(self, mock_repo):
        """--fail-fast flag should be recognized."""
        result = subprocess.run(
            ["bash", str(mock_repo / "scripts" / "validate-local.sh"), "--fail-fast"],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=str(mock_repo),
        )
        # Should not error on unknown flag (exit 126+)
        assert result.returncode in (0, 1), \
            f"--fail-fast not recognized (exit {result.returncode}): {result.stderr}"


class TestNoGitHubActionsWorkflow:
    """Verify the GitHub Actions workflow files have been removed."""

    def test_ci_yml_removed(self):
        ci_path = os.path.join(REPO_ROOT, ".github", "workflows", "ci.yml")
        assert not os.path.exists(ci_path), ".github/workflows/ci.yml should be removed"

    def test_workflows_directory_removed_or_empty(self):
        workflows_path = os.path.join(REPO_ROOT, ".github", "workflows")
        if os.path.isdir(workflows_path):
            files = os.listdir(workflows_path)
            assert len(files) == 0, f".github/workflows/ should be empty, found: {files}"

    def test_ci_automation_doc_mentions_local_validation(self):
        doc_path = os.path.join(REPO_ROOT, "docs", "CI_AUTOMATION.md")
        with open(doc_path, "r") as f:
            content = f.read()
        assert "local" in content.lower(), "Doc should mention local validation"
        assert "validate-local.sh" in content, "Doc should reference validate-local.sh"

    def test_ci_automation_doc_no_stale_github_actions_refs(self):
        """The doc should not reference .github/workflows/ paths as active."""
        doc_path = os.path.join(REPO_ROOT, "docs", "CI_AUTOMATION.md")
        with open(doc_path, "r") as f:
            content = f.read()
        # Should not reference ci.yml as an active workflow file
        assert ".github/workflows/ci.yml" not in content, \
            "Doc should not reference the removed ci.yml workflow"
        assert ".github/workflows/trivy.yml" not in content, \
            "Doc should not reference a removed trivy.yml workflow"
