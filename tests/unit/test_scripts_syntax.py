"""Unit tests: verify shell scripts are syntactically valid."""
import os
import subprocess

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPTS = os.path.join(REPO, "scripts")
SCRIPT_FILES = [
    "upgrade-platform.sh", "rollback.sh",
    "snapshot-helm-baseline.sh", "health-gates.sh",
    "live-tier-smoke.sh", "test-dr-endpoint.sh",
    "tier-load-test.sh", "collect-live-evidence.sh",
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
               "_hg_check_cert_manager", "_hg_check_argocd", "_hg_check_databases",
               "_hg_check_workload_controllers", "_hg_check_storage_and_routes",
               "_hg_check_security_baseline", "_hg_check_helm_releases"]:
        assert kw in content, f"Missing: {kw}"
    assert "containerStatuses" in content
    argocd_gate = content.split("_hg_check_argocd()", 1)[1].split(
        "_hg_check_databases()", 1
    )[0]
    assert 'select(.status.phase != "Succeeded")' in argocd_gate
    assert '.type == "Ready" and .status == "True"' in content
    assert "HEALTH_EXPECTED_NODES" in content
    assert "--config FILE|active" in content
    assert "HEALTH_CONFIG_FILE" in content
    assert ".databases.mongodb.enabled" in content
    assert ".infrastructure.control_plane.count" in content
    assert ".infrastructure.workers.count" in content
    assert 'Unknown argument: $1' in content
    assert 'health-gate-anonymous-probe' in content
    assert '--dry-run=server' in content
    assert '--as=system:anonymous' in content


def test_platform_health_check_derives_exact_profile_node_count():
    with open(os.path.join(REPO, "platform-orchestrator", "platform.sh")) as f:
        content = f.read()
    assert ".infrastructure.control_plane.count" in content
    assert ".infrastructure.workers.count" in content
    assert 'HEALTH_EXPECTED_NODES="$expected_nodes"' in content


def test_live_tier_smoke_covers_foundation_data_paths():
    with open(os.path.join(SCRIPTS, "live-tier-smoke.sh")) as f:
        content = f.read()
    for contract in ["smoke_s3", "smoke_postgresql", "smoke_vault",
                     "smoke_keda_aggregated_api", "external.metrics.k8s.io",
                     "vmsingle-vmsingle", "smoke_logs", "loki-gateway",
                     "ELASTIC_PASSWORD", "_cluster/health", "grafana.monitoring",
                     "argocd-server", "smoke_gateway_routes"]:
        assert contract in content
    assert "--dry-run" in content
    assert "kubectl delete pod" in content
    assert 'kubectl run "$pod" -n default --rm --attach=true --restart=Never' in content
    assert "LIVE_SMOKE_RUN_ID must be a lowercase DNS label" in content
    assert 'vault_exec "$token" kv metadata delete' in content
    assert 'vault_exec "$token" kv metadata get' in content
    gateway = content.split("smoke_gateway_routes()", 1)[1]
    assert "curl -ksS" not in gateway
    assert "2??|3??|400|401|403|405|426" in gateway
    assert "WebSocket-only endpoints" in gateway
    assert 'matches[0].path.value // "/"' in gateway


def test_live_tier_smoke_streams_vault_token_instead_of_using_process_argv(tmp_path):
    with open(os.path.join(SCRIPTS, "live-tier-smoke.sh")) as source:
        content = source.read()
    assert 'env VAULT_TOKEN="$token"' not in content
    assert "IFS= read -r VAULT_TOKEN" in content
    helper = "vault_exec()" + content.split("vault_exec()", 1)[1].split(
        "smoke_vault()", 1
    )[0]
    fake = tmp_path / "kubectl"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >\"$CAPTURE/argv\"\n"
        "IFS= read -r token\n"
        "printf '%s\\n' \"$token\" >\"$CAPTURE/stdin\"\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "-c", helper + '\nvault_exec stdin-only-secret kv get path'],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "CAPTURE": str(tmp_path),
        },
    )
    assert result.returncode == 0, result.stderr
    assert "stdin-only-secret" not in (tmp_path / "argv").read_text()
    assert (tmp_path / "stdin").read_text().strip() == "stdin-only-secret"
