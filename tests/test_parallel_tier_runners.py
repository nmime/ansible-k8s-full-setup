"""Focused contracts for the isolated five-profile campaign runners."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
RUN_TIER = ROOT / "run_tier.sh"
RUN_ALL = ROOT / "run_all.sh"
PROFILES = ("minimal", "small", "medium", "medium-optimized", "production")


def safe_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HCLOUD_TOKEN": "dry-run-token",
            "BACKUP_DR_ACCESS_KEY": "dryaccess",
            "BACKUP_DR_SECRET_KEY": "dry-secret-key-at-least-sixteen",
        }
    )
    return env


def test_one_profile_dry_run_preserves_identity_and_isolates_controller(tmp_path):
    run_root = tmp_path / "state"
    home = tmp_path / "home"
    config = run_root / "runtime.yaml"
    log = run_root / "logs" / "deploy.log"
    result = subprocess.run(
        [
            str(RUN_TIER),
            "medium-optimized",
            "--campaign-id",
            "pytest",
            "--project",
            "t5-pytest-medium-optimized",
            "--domain",
            "medium-optimized.n0xeid.xyz",
            "--location",
            "fsn1",
            "--home",
            str(home),
            "--run-root",
            str(run_root),
            "--config",
            str(config),
            "--log-file",
            str(log),
            "--api-port",
            "17446",
            "--dr-endpoint",
            "https://dr.example.invalid",
            "--dr-bucket",
            "tier-tests",
            "--minimum-storage",
            "--dns-zone",
            "n0xeid.xyz",
            "--certificate-issuer",
            "letsencrypt-staging",
            "--dry-run",
        ],
        cwd=ROOT,
        env=safe_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    profile = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert profile["platform_profile"] == "medium-optimized"
    assert profile["tier"] == "medium"
    assert profile["resource_tier"] == "small"
    assert profile["global"]["project"] == "t5-pytest-medium-optimized"
    assert profile["global"]["campaign_id"] == "pytest"
    assert profile["infrastructure"]["region"] == "fsn1"
    assert profile["backup"]["disaster_recovery"]["prefix"] == (
        "t5-pytest-medium-optimized/velero"
    )
    assert profile["storage"]["size_per_replica"] == "10Gi"
    assert profile["observability"]["pmm"]["storage_size"] == "10Gi"
    assert profile["alerting"]["storage_size"] == "10Gi"
    assert profile["gitlab"]["backup_persistence_enabled"] is False
    assert (run_root / "status.json").is_file()
    status = yaml.safe_load((run_root / "status.json").read_text(encoding="utf-8"))
    assert status["provider_location"] == "fsn1"
    assert "k8s_api_local_port=17446" in result.stdout
    assert f"ssh_key_path={Path.home() / '.ssh' / 'id_ed25519'}" in result.stdout
    assert "ANSIBLE_COLLECTIONS_PATH" in RUN_TIER.read_text(encoding="utf-8")


def test_all_profiles_dry_run_creates_unique_fail_closed_plan(tmp_path):
    campaign_root = tmp_path / "campaign"
    result = subprocess.run(
        [
            str(RUN_ALL),
            "--campaign-id",
            "pytest-all",
            "--campaign-root",
            str(campaign_root),
            "--project-prefix",
            "t5-pytest",
            "--api-port-base",
            "18443",
            "--dr-endpoint",
            "https://dr.example.invalid",
            "--dr-bucket",
            "tier-tests",
            "--minimum-storage",
            "--capacity-family",
            "cpx",
            "--dry-run",
        ],
        cwd=ROOT,
        env=safe_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    manifest_lines = (campaign_root / "manifest.tsv").read_text(encoding="utf-8").splitlines()
    summary_lines = (campaign_root / "summary.tsv").read_text(encoding="utf-8").splitlines()
    assert len(manifest_lines) == 6
    assert len(summary_lines) == 6
    assert all("\tPASS\t0\t" in line for line in summary_lines[1:])

    ports: set[int] = set()
    projects: set[str] = set()
    for profile in PROFILES:
        controller = campaign_root / "controllers" / profile
        config = controller / "state" / "platform.yaml"
        data = yaml.safe_load(config.read_text(encoding="utf-8"))
        assert data["platform_profile"] == profile
        assert data["global"]["domain"] == f"t5-pytest-{profile}.n0xeid.xyz"
        assert data["backup"]["enabled"] is True
        assert data["backup"]["disaster_recovery"]["enabled"] is True
        projects.add(data["global"]["project"])
        status = yaml.safe_load((controller / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["state"] == "planned"
        ports.add(status["api_port"])
        console = (campaign_root / "results" / f"{profile}.console.log").read_text(
            encoding="utf-8"
        )
        expected_size = {
            "minimal": "32",
            "small": "32",
            "medium": "42",
            "medium-optimized": "32",
            "production": "42",
        }[profile]
        assert data["network"]["bastion"]["server_type"] == "cpx22"
        assert data["infrastructure"]["workers"]["type"] == f"cpx{expected_size}"
        assert "hetzner_bastion_type=cpx22" in console
        assert f"hetzner_worker_type=cpx{expected_size}" in console
        if profile == "minimal":
            assert data["infrastructure"]["control_plane"]["type"] == "cpx32"
            assert "hetzner_cp_type=cpx32" in console
        status_types = status["provider_machine_types"]
        assert status_types["bastion"] == data["network"]["bastion"]["server_type"]
        assert status_types["control_plane"] == data["infrastructure"]["control_plane"]["type"]
        assert status_types["worker"] == data["infrastructure"]["workers"]["type"]

    assert len(projects) == 5
    assert ports == set(range(18443, 18448))
    assert "No cloud resources were changed" in result.stdout


@pytest.mark.parametrize(
    ("profile", "environment_issuer", "option_issuer", "expected"),
    (
        ("production", None, None, "letsencrypt-prod"),
        ("small", None, None, "letsencrypt-staging"),
        ("production", "private-issuer", None, "private-issuer"),
        ("production", "private-issuer", "explicit-issuer", "explicit-issuer"),
    ),
)
def test_certificate_issuer_defaults_and_overrides(
    tmp_path, profile, environment_issuer, option_issuer, expected
):
    env = safe_env()
    env.pop("CERT_MANAGER_CLUSTER_ISSUER", None)
    if environment_issuer is not None:
        env["CERT_MANAGER_CLUSTER_ISSUER"] = environment_issuer
    command = [
        str(RUN_TIER),
        profile,
        "--campaign-id",
        "issuer-test",
        "--project",
        f"issuer-test-{profile}",
        "--domain",
        f"{profile}.example.invalid",
        "--dr-endpoint",
        "https://dr.example.invalid",
        "--dr-bucket",
        "issuer-tests",
        "--run-root",
        str(tmp_path / profile / (option_issuer or "default")),
        "--dry-run",
    ]
    if option_issuer is not None:
        command.extend(("--certificate-issuer", option_issuer))
    result = subprocess.run(
        command,
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert f"cert_manager_cluster_issuer={expected}" in result.stdout


def test_certificate_issuer_help_documents_safe_profile_defaults():
    result = subprocess.run(
        [str(RUN_TIER), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "production uses letsencrypt-prod; others staging" in result.stdout


@pytest.mark.parametrize(
    ("family", "bastion", "control_plane", "worker"),
    (
        ("cx", "cx23", "cx33", "cx33"),
        ("cax", "cax11", "cax21", "cax21"),
        ("cpx", "cpx22", "cpx32", "cpx32"),
        ("ccx", "ccx13", "ccx23", "ccx23"),
    ),
)
def test_one_profile_can_plan_every_capacity_tariff(
    tmp_path, family, bastion, control_plane, worker
):
    config = tmp_path / family / "platform.yaml"
    result = subprocess.run(
        [
            str(RUN_TIER),
            "minimal",
            "--campaign-id",
            f"tariff-{family}",
            "--project",
            f"tariff-{family}",
            "--domain",
            f"{family}.example.invalid",
            "--capacity-family",
            family,
            "--run-root",
            str(tmp_path / family),
            "--config",
            str(config),
            "--dry-run",
        ],
        cwd=ROOT,
        env=safe_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    profile = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert profile["network"]["bastion"]["server_type"] == bastion
    assert profile["infrastructure"]["control_plane"]["type"] == control_plane
    assert profile["infrastructure"]["workers"]["type"] == worker


def test_arm_capacity_tariff_is_rejected_before_live_deployment():
    result = subprocess.run(
        [
            str(RUN_TIER),
            "minimal",
            "--capacity-family",
            "cax",
        ],
        cwd=ROOT,
        env=safe_env(),
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert "planning-only" in result.stderr
    assert "ARM64 production attestation" in result.stderr


def test_certificate_issuer_can_be_loaded_from_the_protected_project_env(tmp_path):
    project_env = tmp_path / ".env"
    project_env.write_text("CERT_MANAGER_CLUSTER_ISSUER=env-file-issuer\n", encoding="utf-8")
    project_env.chmod(0o600)
    env = safe_env()
    env.pop("CERT_MANAGER_CLUSTER_ISSUER", None)
    env["PROJECT_ENV_FILE"] = str(project_env)
    result = subprocess.run(
        [
            str(RUN_TIER),
            "production",
            "--campaign-id",
            "issuer-env-file",
            "--project",
            "issuer-env-file-production",
            "--domain",
            "production.example.invalid",
            "--dr-endpoint",
            "https://dr.example.invalid",
            "--dr-bucket",
            "issuer-tests",
            "--run-root",
            str(tmp_path / "state"),
            "--dry-run",
        ],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "cert_manager_cluster_issuer=env-file-issuer" in result.stdout


def test_runner_source_declares_all_profiles_and_parallel_waits():
    tier_source = RUN_TIER.read_text(encoding="utf-8")
    all_source = RUN_ALL.read_text(encoding="utf-8")
    assert "minimal small medium medium-optimized production" in tier_source
    assert 'SUPPORTED_PROFILES="minimal small medium medium-optimized production"' in tier_source
    assert "git -C \"$SCRIPT_DIR\" worktree add --detach" in all_source
    assert ") >\"$console_log\" 2>&1 &" in all_source
    assert "No automatic teardown was attempted" in all_source
    assert "worktree remove" in all_source
    assert "--skip-kubespray" in tier_source
    assert 'DEPLOY_ARGS+=(-e "skip_kubespray=true")' in tier_source
    assert "--skip-kubespray" in all_source
    assert 'export ANSIBLE_FORKS="$CONTROLLER_FORKS"' in tier_source
    assert '--controller-forks "$CONTROLLER_FORKS"' in all_source
    assert 'CONTROLLER_FORKS="${CONTROLLER_FORKS:-1}"' in all_source
    assert '--operator-state-root "${SCRIPT_DIR}/.campaign-state/${project}"' in all_source
    assert 'vault_init_output_file=${OPERATOR_STATE_ROOT}/.vault-init-${PROJECT}.json' in tier_source
    assert 'secrets_file=${OPERATOR_STATE_ROOT}/.platform-secrets.yml' in tier_source
