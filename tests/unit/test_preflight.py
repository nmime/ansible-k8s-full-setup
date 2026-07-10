"""Unit tests for scripts/preflight_check.py"""
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS = os.path.join(os.path.dirname(__file__), "..", "..", "scripts")
sys.path.insert(0, SCRIPTS)
from preflight_check import CheckResult, PreflightReport, run_all, run

class TestCheckResult:
    def test_pass(self):
        c = CheckResult("test", True, "ok")
        assert c.passed is True
        assert c.level == "info"

    def test_fail_error(self):
        c = CheckResult("test", False, "bad", "error")
        assert c.passed is False
        assert c.level == "error"

    def test_warn(self):
        c = CheckResult("test", True, "w", "warn")
        assert c.level == "warn"

class TestPreflightReport:
    def test_empty_passed(self):
        r = PreflightReport()
        assert r.passed() is True
        assert r.failures == 0
        assert r.warnings == 0

    def test_failures_counted(self):
        r = PreflightReport(checks=[
            CheckResult("a", False, "x", "error"),
            CheckResult("b", True, "ok"),
            CheckResult("c", False, "x", "error"),
        ])
        assert r.failures == 2
        assert r.passed() is False

    def test_warnings_not_fatal(self):
        r = PreflightReport(checks=[
            CheckResult("a", True, "ok"),
            CheckResult("b", True, "w", "warn"),
        ])
        assert r.warnings == 1
        assert r.passed() is True

class TestRun:
    def test_success(self):
        r = run(["echo", "hello"])
        assert r.returncode == 0
        assert "hello" in r.stdout

    def test_not_found(self):
        r = run(["_nonexistent_cmd_xyz_"])
        assert r.returncode == 127

class TestRunAllDry:
    def test_dry_run_all_pass(self):
        report = run_all("/tmp", dry_run=True)
        assert len(report.checks) > 0
        for c in report.checks:
            assert c.passed is True, f"Expected pass: {c.message}"

    def test_dry_run_report_passed(self):
        report = run_all("/tmp", dry_run=True)
        assert report.passed() is True

    def test_dry_run_has_categories(self):
        report = run_all("/tmp", dry_run=True)
        names = [c.name for c in report.checks]
        cats = {n.split(":")[0] for n in names}
        for cat in ["tool", "cluster", "helm", "nodes", "disk", "snapshot", "git", "config"]:
            assert cat in cats, f"Missing category: {cat}"

class TestConfigCheck:
    def test_valid_config(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / "platform.yaml"
            cfg.write_text("global:\n  domain: test.example.com\n  email: a@b.com\n")
            from preflight_check import check_config
            report = PreflightReport()
            check_config(report, dry_run=False, config_file=str(cfg))
            assert report.failures == 0, f"Failures: {[c.message for c in report.checks if not c.passed]}"

    def test_missing_config(self):
        from preflight_check import check_config
        report = PreflightReport()
        check_config(report, dry_run=False, config_file="/nonexistent/platform.yaml")
        assert report.failures == 1
