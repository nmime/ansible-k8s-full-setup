"""E2E tests for upgrade orchestration (dry-run / simulation only)."""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO, "scripts")

def _run(cmd, timeout=30):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO)

class TestE2EPreflight:
    def test_dry_run_exits_zero(self):
        r = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--dry-run", "true"])
        assert r.returncode == 0, f"Exit {r.returncode}: {r.stderr}"

    def test_output_has_summary(self):
        r = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--dry-run", "true"])
        assert "checks" in r.stdout.lower()

    def test_output_has_categories(self):
        r = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--dry-run", "true"])
        for cat in ["tool:", "cluster:", "snapshot:"]:
            assert cat in r.stdout, f"Missing category: {cat}"

class TestE2ESnapshotFlow:
    def test_snapshot_creates_dir(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = os.path.join(td, "snapshot")
            os.makedirs(snap_dir)
            r = subprocess.run(["bash", "-c", f"""
SNAPSHOT_DIR={snap_dir}
source {SCRIPTS}/snapshot-helm-baseline.sh
SNAPSHOT_DRY_RUN=true
snap=$(capture_snapshot)
echo "$snap"
"""], capture_output=True, text=True, timeout=10)
            assert r.returncode == 0
            assert snap_dir in r.stdout

    def test_snapshot_cli_copies_explicit_config_to_explicit_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            config = root / "generated-source.yaml"
            config.write_text("global:\n  project: isolated-migration\n")
            snapshot_root = root / "migration-state" / "rollback-snapshots"
            fake_bin = root / "bin"
            fake_bin.mkdir()
            helm = fake_bin / "helm"
            helm.write_text(
                "#!/bin/sh\n"
                "if [ \"$1\" = list ]; then printf '[]\\n'; fi\n"
            )
            helm.chmod(0o755)
            kubectl = fake_bin / "kubectl"
            kubectl.write_text("#!/bin/sh\nexit 0\n")
            kubectl.chmod(0o755)
            env = os.environ.copy()
            env["PATH"] = f"{fake_bin}:{env['PATH']}"
            r = subprocess.run(
                [
                    "bash", os.path.join(SCRIPTS, "snapshot-helm-baseline.sh"),
                    "--config", str(config),
                    "--snapshot-dir", str(snapshot_root),
                ],
                capture_output=True, text=True, timeout=10, env=env,
            )
            assert r.returncode == 0, r.stderr
            snapshot = Path(r.stdout.strip().splitlines()[-1])
            assert snapshot.parent == snapshot_root
            assert (snapshot / "platform.yaml").read_text() == config.read_text()
            assert str(config) in (snapshot / "MANIFEST.yaml").read_text()

    def test_rollback_locates_snapshot(self):
        with tempfile.TemporaryDirectory() as td:
            snap_dir = os.path.join(td, "snapshot")
            os.makedirs(snap_dir)
            test_snap = os.path.join(snap_dir, "upgrade-20250101-120000")
            os.makedirs(test_snap)
            Path(test_snap, "MANIFEST.yaml").write_text("snapshot_time: test")
            r = subprocess.run(["bash", "-c", f"""
SNAPSHOT_DIR={snap_dir}
PROJECT_ROOT={td}
export SNAPSHOT_DIR PROJECT_ROOT
mkdir -p "$PROJECT_ROOT/logs"
source {SCRIPTS}/rollback.sh
SNAPSHOT_DIR={snap_dir}
find_latest_snapshot
"""], capture_output=True, text=True, timeout=10)
            assert r.returncode == 0, f"stderr: {r.stderr}"
            assert "upgrade-20250101-120000" in r.stdout

class TestE2ERollbackFlow:
    def test_rollback_help(self):
        r = _run(["bash", os.path.join(SCRIPTS, "rollback.sh"), "-h"])
        output = r.stdout + r.stderr
        assert "component" in output.lower() or "tier" in output.lower()

class TestE2EUpgradeFlags:
    def test_help_shows_all_commands(self):
        r = _run(["bash", os.path.join(SCRIPTS, "upgrade-platform.sh"), "-h"])
        output = r.stdout + r.stderr
        for cmd in ["plan", "execute", "preflight", "snapshot", "validate"]:
            assert cmd in output, f"Missing: {cmd}"

    def test_missing_command_errors(self):
        r = _run(["bash", os.path.join(SCRIPTS, "upgrade-platform.sh")])
        assert r.returncode != 0

class TestE2ERunbook:
    def test_runbook_exists(self):
        assert os.path.isfile(os.path.join(REPO, "UPGRADE_RUNBOOK.md"))

    def test_runbook_sections(self):
        with open(os.path.join(REPO, "UPGRADE_RUNBOOK.md")) as f:
            content = f.read()
        for section in ["Preflight", "Rollback", "Health Gates", "Canary",
                         "Snapshot", "Dry-Run", "Troubleshooting", "Quick Reference"]:
            assert section in content, f"Missing section: {section}"

class TestE2EFullPipeline:
    def test_full_pipeline_dry_run(self):
        r1 = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--dry-run", "true"])
        assert r1.returncode == 0, f"Preflight: {r1.stderr}"
        r2 = _run(["bash", "-c", f"source {SCRIPTS}/snapshot-helm-baseline.sh; SNAPSHOT_DRY_RUN=true; capture_snapshot"])
        assert r2.returncode == 0, f"Snapshot: {r2.stderr}"
        r3 = _run(["bash", "-c", f"source {SCRIPTS}/health-gates.sh; HEALTH_DRY_RUN=true; check_health_gates"])
        assert r3.returncode in (0, 1)

    def test_all_test_dirs_have_tests(self):
        for d in ["tests/unit", "tests/component", "tests/e2e"]:
            path = os.path.join(REPO, d)
            assert os.path.isdir(path)
            test_files = [f for f in os.listdir(path) if f.startswith("test_")]
            assert len(test_files) > 0, f"No test files in {d}"
