"""Component tests: verify integration between upgrade scripts."""
import os
import subprocess
import sys

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO, "scripts")

def _run(cmd, timeout=15):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=REPO)

class TestScriptIntegration:
    def test_preflight_dry_run(self):
        r = _run([sys.executable, "-c",
                   f"import sys; sys.path.insert(0,'{SCRIPTS}'); "
                   f"from preflight_check import run_all; "
                   f"r = run_all('{REPO}', dry_run=True); print(r.passed())"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "True" in r.stdout

    def test_all_scripts_syntax(self):
        for name in ["upgrade-platform.sh", "rollback.sh", "snapshot-helm-baseline.sh", "health-gates.sh"]:
            r = subprocess.run(["bash", "-n", os.path.join(SCRIPTS, name)], capture_output=True, text=True)
            assert r.returncode == 0, f"Syntax: {name}: {r.stderr}"

class TestSnapshotRollbackRoundTrip:
    def test_rollback_finds_snapshot(self):
        import tempfile, shutil
        td = tempfile.mkdtemp()
        try:
            snap_dir = os.path.join(td, "snapshot")
            os.makedirs(snap_dir)
            test_snap = os.path.join(snap_dir, "upgrade-test-20250101")
            os.makedirs(test_snap)
            with open(os.path.join(test_snap, "MANIFEST.yaml"), "w") as f:
                f.write("snapshot_time: test")
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
            assert "upgrade-test-20250101" in r.stdout
        finally:
            shutil.rmtree(td, ignore_errors=True)

class TestHealthGatesSourced:
    def test_health_gates_dry_run(self):
        r = _run(["bash", "-c", f"""
source {SCRIPTS}/health-gates.sh
HEALTH_DRY_RUN=true
check_health_gates 2>&1
"""])
        output = r.stdout + r.stderr
        for comp in ["Nodes", "Cilium", "Cert-manager", "ArgoCD", "Databases",
                     "workload controllers", "Persistent storage", "Runtime security", "Helm releases"]:
            assert comp in output, f"Missing: {comp}"

class TestUpgradeFlags:
    def test_help_shows_commands(self):
        r = _run(["bash", os.path.join(SCRIPTS, "upgrade-platform.sh"), "-h"])
        output = r.stdout + r.stderr
        for kw in ["plan", "execute", "preflight", "snapshot", "validate", "--dry-run"]:
            assert kw in output, f"Missing in help: {kw}"

    def test_missing_command_errors(self):
        r = _run(["bash", os.path.join(SCRIPTS, "upgrade-platform.sh")])
        assert r.returncode != 0

class TestPreflightStandalone:
    def test_help(self):
        r = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--help"])
        assert r.returncode == 0

    def test_dry_run(self):
        r = _run([sys.executable, os.path.join(SCRIPTS, "preflight_check.py"), "--dry-run", "true"])
        assert r.returncode == 0, f"stderr: {r.stderr}"
        assert "PREFLIGHT REPORT" in r.stdout or "checks" in r.stdout.lower()
