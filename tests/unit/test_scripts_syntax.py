"""Unit tests: verify shell scripts are syntactically valid."""
import os
import subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO, "scripts")
SCRIPT_FILES = [
    "upgrade-platform.sh", "rollback.sh",
    "snapshot-helm-baseline.sh", "health-gates.sh",
]

def test_all_scripts_exist():
    for name in SCRIPT_FILES:
        assert os.path.isfile(os.path.join(SCRIPTS, name)), f"Missing: {name}"

def test_all_scripts_executable():
    for name in SCRIPT_FILES:
        assert os.access(os.path.join(SCRIPTS, name), os.X_OK), f"Not executable: {name}"

def test_all_scripts_syntax_valid():
    for name in SCRIPT_FILES:
        r = subprocess.run(["bash", "-n", os.path.join(SCRIPTS, name)], capture_output=True, text=True)
        assert r.returncode == 0, f"Syntax error in {name}: {r.stderr}"

def test_upgrade_platform_has_usage():
    with open(os.path.join(SCRIPTS, "upgrade-platform.sh")) as f:
        content = f.read()
    for kw in ["plan", "execute", "preflight", "snapshot", "validate", "--dry-run", "--tier", "--component"]:
        assert kw in content, f"Missing keyword: {kw}"

def test_rollback_has_components():
    with open(os.path.join(SCRIPTS, "rollback.sh")) as f:
        content = f.read()
    assert "rollback_component" in content
    assert "--component" in content
    assert "--tier" in content
    assert "--force" in content
    for comp in ["argocd", "cilium", "cert-manager", "database", "observability", "gitlab"]:
        assert comp in content, f"Missing component handler: {comp}"

def test_snapshot_has_capture():
    with open(os.path.join(SCRIPTS, "snapshot-helm-baseline.sh")) as f:
        content = f.read()
    for kw in ["capture_snapshot", "helm-values", "crds.yaml", "MANIFEST.yaml"]:
        assert kw in content, f"Missing: {kw}"

def test_health_gates_has_all():
    with open(os.path.join(SCRIPTS, "health-gates.sh")) as f:
        content = f.read()
    for kw in ["check_health_gates", "_hg_check_nodes", "_hg_check_cilium",
               "_hg_check_cert_manager", "_hg_check_argocd", "_hg_check_databases"]:
        assert kw in content, f"Missing: {kw}"
    assert "containerStatuses" in content
    assert '.type == "Ready" and .status == "True"' in content
