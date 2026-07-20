"""Focused contracts for the isolated five-profile campaign runners."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
    assert profile["backup"]["disaster_recovery"]["prefix"] == (
        "t5-pytest-medium-optimized/velero"
    )
    assert profile["storage"]["size_per_replica"] == "10Gi"
    assert profile["observability"]["pmm"]["storage_size"] == "10Gi"
    assert (run_root / "status.json").is_file()
    assert "k8s_api_local_port=17446" in result.stdout
    assert f"ssh_key_path={Path.home() / '.ssh' / 'id_ed25519'}" in result.stdout


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
        projects.add(data["global"]["project"])
        status = yaml.safe_load((controller / "state" / "status.json").read_text(encoding="utf-8"))
        assert status["state"] == "planned"
        ports.add(status["api_port"])

    assert len(projects) == 5
    assert ports == set(range(18443, 18448))
    assert "No cloud resources were changed" in result.stdout


def test_runner_source_declares_all_profiles_and_parallel_waits():
    tier_source = RUN_TIER.read_text(encoding="utf-8")
    all_source = RUN_ALL.read_text(encoding="utf-8")
    assert "minimal small medium medium-optimized production" in tier_source
    assert 'SUPPORTED_PROFILES="minimal small medium medium-optimized production"' in tier_source
    assert "git -C \"$SCRIPT_DIR\" worktree add --detach" in all_source
    assert ") >\"$console_log\" 2>&1 &" in all_source
    assert "No automatic teardown was attempted" in all_source
    assert "worktree remove" in all_source
