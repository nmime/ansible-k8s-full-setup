"""Security and integration tests for the project-local environment loader."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LOADER = ROOT / "scripts" / "load-project-env.sh"


def _run_loader(env_file: Path, command: str, extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    env.update(extra_env or {})
    env["PROJECT_ENV_FILE"] = str(env_file)
    return subprocess.run(
        [
            "bash",
            "-c",
            f'source "$1" || exit $?; {command}',
            "loader-test",
            str(LOADER),
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _write_env(path: Path, content: str, mode: int = 0o600):
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def test_loader_reads_plain_quoted_exported_and_empty_values(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(
        env_file,
        "PLAIN_VALUE=loaded\nQUOTED_VALUE=\"two words\"\nexport EXPORTED_VALUE=yes\nEMPTY_VALUE=\n",
    )
    env = os.environ.copy()
    for key in ("PLAIN_VALUE", "QUOTED_VALUE", "EXPORTED_VALUE", "EMPTY_VALUE"):
        env.pop(key, None)
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; printf "%s|%s|%s|%s" "$PLAIN_VALUE" "$QUOTED_VALUE" "$EXPORTED_VALUE" "$EMPTY_VALUE"',
            "loader-test",
            str(LOADER),
        ],
        cwd=ROOT,
        env={**env, "PROJECT_ENV_FILE": str(env_file)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == "loaded|two words|yes|"


def test_explicit_process_environment_wins(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, "HCLOUD_TOKEN=from-file\n")
    result = _run_loader(env_file, 'printf "%s" "$HCLOUD_TOKEN"', {"HCLOUD_TOKEN": "external"})
    assert result.returncode == 0, result.stderr
    assert result.stdout == "external"


def test_loader_rejects_group_or_world_access(tmp_path):
    env_file = tmp_path / ".env"
    _write_env(env_file, "SAFE_VALUE=yes\n", mode=0o644)
    result = _run_loader(env_file, "true")
    assert result.returncode != 0
    assert "must not be readable or writable by group/others" in result.stderr


def test_loader_does_not_evaluate_shell_syntax(tmp_path):
    env_file = tmp_path / ".env"
    marker = tmp_path / "must-not-exist"
    _write_env(env_file, f"SAFE_VALUE=$(touch {marker})\n")
    result = _run_loader(env_file, 'printf "%s" "$SAFE_VALUE"')
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"$(touch {marker})"
    assert not marker.exists()


def test_all_operational_entrypoints_load_project_env():
    entrypoints = [
        ROOT / "run_all.sh",
        ROOT / "run_tier.sh",
        ROOT / "teardown.sh",
        ROOT / "platform-orchestrator" / "platform.sh",
        *(
            ROOT / "scripts" / name
            for name in (
                "backup-all.sh",
                "cluster-backup.sh",
                "cluster-restore.sh",
                "cycle-test.sh",
                "gitlab-restore-test.sh",
                "gitlab-upgrade-check.sh",
                "migrate-profile.sh",
                "mongodb-restore-drill.sh",
                "pg-restore-drill.sh",
                "pg-upgrade-check.sh",
                "restore-drill.sh",
                "rollback.sh",
                "snapshot-helm-baseline.sh",
                "upgrade-platform.sh",
                "vault-restore-drill.sh",
                "vault-upgrade-check.sh",
            )
        ),
    ]
    for entrypoint in entrypoints:
        assert "load-project-env.sh" in entrypoint.read_text(encoding="utf-8"), entrypoint


def test_project_env_is_gitignored():
    result = subprocess.run(
        ["git", "check-ignore", ".env"], cwd=ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0
