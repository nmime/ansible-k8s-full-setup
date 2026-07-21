"""Contracts for bounded, profile-aware live load evidence."""

import json
import os
from pathlib import Path
import re
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
LOAD = ROOT / "scripts" / "tier-load-test.sh"
EVIDENCE = ROOT / "scripts" / "collect-live-evidence.sh"
PROFILES = ROOT / "platform-orchestrator" / "profiles"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, cwd=ROOT, text=True, capture_output=True, check=False)


def function_source(content: str, name: str, next_name: str) -> str:
    return name + "()" + content.split(name + "()", 1)[1].split(next_name + "()", 1)[0]


def test_load_harness_scripts_are_executable_and_valid_bash():
    for script in (LOAD, EVIDENCE):
        assert script.is_file()
        assert os.access(script, os.X_OK)
        result = run("bash", "-n", str(script))
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("profile", "clients", "dragonfly_enabled"),
    [
        ("minimal", 2, False),
        ("small", 4, True),
        ("medium-optimized", 8, True),
        ("medium", 12, True),
        ("production", 20, True),
    ],
)
def test_every_named_profile_has_a_machine_readable_dry_run(
    tmp_path: Path, profile: str, clients: int, dragonfly_enabled: bool
):
    output = tmp_path / profile
    result = run(
        str(LOAD), "--config", str(PROFILES / f"{profile}.yaml"),
        "--output", str(output), "--run-id", f"test-{profile}", "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["schema"] == "tier-load-test/v1"
    assert summary["profile"] == profile
    assert summary["dry_run"] is True
    assert summary["limits"]["clients"] == clients
    phases = {phase["phase"]: phase for phase in summary["phases"]}
    assert set(phases) == {"http", "s3", "postgresql", "vault", "dragonfly"}
    assert phases["http"]["status"] == "planned"
    assert phases["dragonfly"]["enabled"] is dragonfly_enabled
    assert phases["dragonfly"]["status"] == ("planned" if dragonfly_enabled else "skipped")
    assert (output / "phases.tsv").read_text().startswith("phase\tenabled\tstatus\t")
    assert (output / "evidence" / "baseline" / "evidence.json").is_file()
    assert (output / "evidence" / "final" / "evidence.json").is_file()


def test_evidence_dry_run_writes_json_and_tsv_without_kubeconfig(tmp_path: Path):
    output = tmp_path / "evidence"
    result = run(
        str(EVIDENCE), "--config", str(PROFILES / "production.yaml"),
        "--kubeconfig", "/definitely/not/a/kubeconfig", "--output", str(output),
        "--stage", "baseline", "--dry-run",
    )
    assert result.returncode == 0, result.stderr
    evidence = json.loads((output / "evidence.json").read_text())
    assert evidence["schema"] == "tier-live-evidence/v1"
    assert evidence["profile"] == "production"
    assert evidence["expected_nodes"] == 6
    for filename in ("resources.tsv", "top-nodes.tsv", "top-pods.tsv", "warning-events.tsv"):
        assert (output / filename).is_file()


def test_harness_pins_every_probe_image_and_never_uses_latest():
    content = LOAD.read_text()
    expected = {
        "HTTP_IMAGE": "curlimages/curl:8.17.0",
        "S3_IMAGE": "amazon/aws-cli:2.34.48",
        "POSTGRES_IMAGE": "postgres:18.2-alpine3.23",
        "VAULT_IMAGE": "hashicorp/vault:2.0.3",
        "DRAGONFLY_IMAGE": "redis:7.4.7-alpine3.21",
    }
    for variable, image in expected.items():
        assert f'{variable}="{image}"' in content
    assert not re.search(r"image:\s*[^\s]+:latest\b", content)
    assert "actual_image" in content and "refusing Vault load against unpinned image" in content


def test_harness_has_bounded_hard_stops_and_cleanup_for_every_mutating_system():
    content = LOAD.read_text()
    for contract in (
        "PHASE_TIMEOUT=900", "MAX_ERROR_PERCENT", "MAX_RESTART_DELTA",
        "activeDeadlineSeconds", "backoffLimit: 0", "hard stop: phase timeout",
        "assert_cluster_safe", "cleanup_s3", "cleanup_postgresql", "cleanup_vault",
        "cleanup_dragonfly", "trap 'cleanup_all",
    ):
        assert contract in content
    assert "errors*10000" in content
    assert "DROP TABLE IF EXISTS" in content
    assert "kv metadata delete" in content
    assert "kv metadata get" in content
    assert "prefix=s3://backups/tier-load/" in content
    assert "job still exists" in content
    assert "--wait=true" in content
    assert "if ! cleanup_job; then" in content
    assert "--request-timeout=30s" in content
    assert "automountServiceAccountToken: false" in content
    assert "automountServiceAccountToken:false" in content
    assert content.count("automountServiceAccountToken") == 6
    assert '-a "\\$PASSWORD"' not in content
    assert "xargs -r" not in content
    assert 'PATH="$ROOT_DIR/.venv/bin:$PATH"' in content
    assert "restart_changes=$(jq" in content
    assert "pod_uid" in EVIDENCE.read_text()
    restart_gate = content.split("restart_changes=$(jq", 1)[1].split("')", 1)[0]
    assert 'if $b[.key] == null then 0' in restart_gate
    assert 'select($a[.key] == null)' not in restart_gate
    assert "same-name identity/restart changes" in content
    assert "curl -ksS" not in function_source(content, "phase_http", "phase_s3")


def test_postgresql_and_dragonfly_load_verify_exact_operations_and_round_trip():
    content = LOAD.read_text()
    postgresql = function_source(content, "phase_postgresql", "phase_vault")
    dragonfly = function_source(content, "phase_dragonfly", "run_phase")

    assert r"base=\$((TRANSACTIONS/CLIENTS))" in postgresql
    assert r'[ "\$processed" -eq "\$TRANSACTIONS" ]' in postgresql
    assert "per_client=" not in postgresql
    assert r'redis-cli -h dragonfly get "\$key"' in dragonfly
    assert "redis-cli -h dragonfly --raw" in dragonfly
    assert 'printf \'SET %s %s\\nGET %s\\n\'' in dragonfly
    assert "/tmp/redis-result." in dragonfly
    assert "actual_lines" in dragonfly
    assert r'redis-cli -h dragonfly exists "\$key"' in dragonfly


def test_failed_job_deletion_is_reported_and_ownership_is_preserved(tmp_path: Path):
    content = LOAD.read_text()
    cleanup = function_source(content, "cleanup_job", "cleanup_s3")
    fake = tmp_path / "kubectl"
    fake.write_text("#!/bin/sh\n[ \"$1\" = delete ] && exit 1\nexit 0\n")
    fake.chmod(0o755)
    result = subprocess.run(
        [
            "bash", "-c",
            cleanup + '\nlog(){ :; }\nDRY_RUN=false\nK=("$1")\n'
            'active_job_ns=storage\nactive_job_name=tier-load-test\n'
            'cleanup_job; rc=$?; printf "%s:%s:%s\\n" "$rc" "$active_job_ns" "$active_job_name"',
            "bash", str(fake),
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.stdout.strip() == "1:storage:tier-load-test"


def test_vault_cleanup_rejects_unverifiable_deletion(tmp_path: Path):
    content = LOAD.read_text()
    cleanup = function_source(content, "cleanup_vault", "cleanup_dragonfly")
    fake = tmp_path / "kubectl"
    fake.write_text("#!/bin/sh\necho 'connection refused' >&2\nexit 1\n")
    fake.chmod(0o755)
    result = subprocess.run(
        [
            "bash", "-c",
            cleanup + '\nlog(){ :; }\nvault_started=true\nvault_token=redacted\n'
            'CLIENTS=1\nRUN_ID=test\nK=("$1")\ncleanup_vault',
            "bash", str(fake),
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 1


def test_vault_cleanup_accepts_only_verified_absence(tmp_path: Path):
    content = LOAD.read_text()
    cleanup = function_source(content, "cleanup_vault", "cleanup_dragonfly")
    fake = tmp_path / "kubectl"
    fake.write_text("#!/bin/sh\necho 'No value found at secret/metadata/tier-load/test/1' >&2\nexit 2\n")
    fake.chmod(0o755)
    result = subprocess.run(
        [
            "bash", "-c",
            cleanup + '\nlog(){ :; }\nvault_started=true\nvault_token=redacted\n'
            'CLIENTS=1\nRUN_ID=test\nK=("$1")\ncleanup_vault',
            "bash", str(fake),
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.returncode == 0


def test_vault_token_is_streamed_over_stdin_and_never_put_in_kubectl_argv(tmp_path: Path):
    content = LOAD.read_text()
    cleanup = function_source(content, "cleanup_vault", "cleanup_dragonfly")
    assert 'VAULT_TOKEN="$vault_token"' not in content
    assert 'env VAULT_TOKEN=' not in content
    assert content.count("printf '%s\\n' \"$vault_token\" |") >= 3
    assert "IFS= read -r VAULT_TOKEN" in content
    assert "export VAULT_TOKEN" in content

    fake = tmp_path / "kubectl"
    fake.write_text(
        "#!/bin/sh\n"
        "printf '%s\\n' \"$*\" >>\"$CAPTURE_DIR/argv\"\n"
        "IFS= read -r token\n"
        "printf '%s\\n' \"$token\" >>\"$CAPTURE_DIR/stdin\"\n"
        "echo 'No value found at secret/metadata/tier-load/test/1' >&2\n"
        "exit 2\n"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        [
            "bash", "-c",
            cleanup + '\nlog(){ :; }\nvault_started=true\nvault_token=stdin-only-secret\n'
            'CLIENTS=1\nRUN_ID=test\nK=("$1")\ncleanup_vault',
            "bash", str(fake),
        ],
        text=True, capture_output=True, check=False,
        env={**os.environ, "CAPTURE_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    assert "stdin-only-secret" not in (tmp_path / "argv").read_text()
    assert (tmp_path / "stdin").read_text().splitlines() == [
        "stdin-only-secret", "stdin-only-secret"
    ]


def test_signal_handler_runs_cleanup_before_returning_signal_status():
    content = LOAD.read_text()
    handler = function_source(content, "on_signal", "collect_evidence")
    result = subprocess.run(
        [
            "bash", "-c",
            handler + '\ncleanup_all(){ echo cleaned; }\non_signal 143',
        ],
        text=True, capture_output=True, check=False,
    )
    assert result.stdout.strip() == "cleaned"
    assert result.returncode == 143


def test_evidence_collector_is_read_only_and_secret_free_by_contract():
    content = EVIDENCE.read_text()
    assert 'source "${SCRIPT_DIR}/load-project-env.sh"' in content
    for resource in (
        "get nodes", "get pods", "get deployments", "get statefulsets",
        "get daemonsets", "get jobs", "get pvc", "get certificates",
        "get httproutes", "top nodes", "top pods",
    ):
        assert resource in content
    assert "get secret" not in content
    assert "get secrets" not in content
    assert "kubectl apply" not in content
    assert "kubectl delete" not in content
    assert "token \\u0027[REDACTED]\\u0027" in content
    assert "credential=[REDACTED]" in content
    assert "Port could not be cast to integer value as \\u0027[REDACTED]\\u0027" in content
    assert "lb_required=" in content
    assert "for attempt in 1 2 3 4 5 6 7 8 9 10" in content
    assert "$provider_edge.required|not" in content
    assert "$provider_edge.checked and $provider_edge.present and $provider_edge.healthy" in content
    assert '(.metadata.ownerReferences[0].kind // "") != "Job"' in content
    assert '.status.phase=="Succeeded" or .status.phase=="Failed"' in content


def test_evidence_api_discovery_consumes_pipelines_under_pipefail():
    content = EVIDENCE.read_text()
    assert "grep -qx 'certificates.cert-manager.io'" not in content
    assert "grep -qx 'httproutes.gateway.networking.k8s.io'" not in content
    assert "grep -Fx 'certificates.cert-manager.io' >/dev/null" in content
    assert "grep -Fx 'httproutes.gateway.networking.k8s.io' >/dev/null" in content


def test_evidence_preserves_explicit_false_load_balancer_setting():
    content = EVIDENCE.read_text()
    assert "lb_required=$(yq -r '.network.load_balancer.enabled'" in content
    assert '[[ "$lb_required" == null ]] && lb_required=true' in content


def test_load_generator_scales_s3_memory_and_waits_for_controller_convergence():
    content = LOAD.read_text()
    assert "s3_memory_limit_mi=$((256 + CLIENTS * 96))" in content
    assert 'memory: "${s3_memory_limit_mi}Mi"' in content
    assert "s3_cpu_limit=$CLIENTS" in content
    assert "(( s3_cpu_limit <= 4 )) || s3_cpu_limit=4" in content
    assert 'limits: {cpu: "$s3_cpu_limit"' in content
    assert "collect_settled_evidence()" in content
    assert "for attempt in 1 2 3 4 5 6 7" in content
    settled = content.split("collect_settled_evidence()", 1)[1].split("execute_job()", 1)[0]
    assert "printf '%s\\n' \"$evidence\"" in settled


def test_dragonfly_load_uses_env_auth_and_verifies_every_worker():
    content = LOAD.read_text()
    assert 'export REDISCLI_AUTH="\\$PASSWORD"' in content
    assert "redis-cli -h dragonfly --raw" in content
    assert "for result in /tmp/redis-result.*" in content
    assert '-a "\\$PASSWORD"' not in content


def test_evidence_redacts_url_parser_credential_fragments():
    content = EVIDENCE.read_text()
    redact = "def redact:" + content.split("| jq -r 'def redact:", 1)[1].split(
        "    .items[]", 1
    )[0]
    fragment = "generated@credential/fragment"
    result = run(
        "jq", "-nr", "--arg", "message",
        f"ValueError: Port could not be cast to integer value as '{fragment}'",
        redact + "\n$message | redact",
    )
    assert result.returncode == 0, result.stderr
    assert fragment not in result.stdout
    assert result.stdout.strip().endswith(
        "Port could not be cast to integer value as '[REDACTED]'"
    )


@pytest.mark.parametrize(
    "arguments",
    [
        ("--clients", "0"), ("--clients", "65"), ("--phase-timeout", "29"),
        ("--max-error-percent", "20.01"),
        ("--run-id", "Test-1"),
        ("--run-id", "trailing-"),
        ("--http-url", "https://safe.example/\nbad: value"),
    ],
)
def test_unsafe_load_bounds_fail_before_cluster_access(tmp_path: Path, arguments: tuple[str, str]):
    result = run(
        str(LOAD), "--config", str(PROFILES / "minimal.yaml"),
        "--output", str(tmp_path / "invalid"), *arguments, "--dry-run",
    )
    assert result.returncode == 2
