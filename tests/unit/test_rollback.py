"""Unit tests for rollback.sh script structure and logic."""
import os
import subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ROLLBACK = os.path.join(REPO, "scripts", "rollback.sh")

def test_file_exists():
    assert os.path.isfile(ROLLBACK)

def test_executable():
    assert os.access(ROLLBACK, os.X_OK)

def test_syntax_valid():
    r = subprocess.run(["bash", "-n", ROLLBACK], capture_output=True, text=True)
    assert r.returncode == 0, f"Syntax: {r.stderr}"

def test_set_euo():
    with open(ROLLBACK) as f:
        assert "set -euo pipefail" in f.read()

def test_find_latest_snapshot():
    with open(ROLLBACK) as f:
        assert "find_latest_snapshot" in f.read()

def test_rollback_component():
    with open(ROLLBACK) as f:
        assert "rollback_component" in f.read()

def test_rollback_tier():
    with open(ROLLBACK) as f:
        assert "rollback_tier" in f.read()

def test_all_components():
    with open(ROLLBACK) as f:
        c = f.read()
    for comp in ["argocd", "cilium", "cert-manager", "database", "observability", "gitlab"]:
        assert comp in c, f"Missing: {comp}"

def test_post_rollback_health():
    with open(ROLLBACK) as f:
        assert "health-gates" in f.read()

def test_state_recording():
    with open(ROLLBACK) as f:
        assert "rollback-complete.json" in f.read()

def test_flags():
    with open(ROLLBACK) as f:
        c = f.read()
    for flag in ["--dry-run", "--tier", "--force", "--component", "--snapshot"]:
        assert flag in c, f"Missing flag: {flag}"
