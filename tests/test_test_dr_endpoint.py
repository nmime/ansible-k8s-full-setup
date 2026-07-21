"""Fail-closed contracts for the disposable persistent DR endpoint."""

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "test-dr-endpoint.sh"


def _fake_hcloud(tmp_path: Path) -> tuple[Path, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "hcloud.calls"
    executable = bin_dir / "hcloud"
    executable.write_text(
        """#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$HCLOUD_CALLS"
case "$1 $2" in
  "volume describe")
    printf '%s\\n' '{"id":123,"labels":{"managed-by":"test-dr-endpoint","campaign":"lab01","purpose":"dr-object-storage"},"location":{"name":"hel1"},"server":null,"protection":{"delete":true}}'
    ;;
  "server describe"|"firewall describe"|"ssh-key describe") exit 1 ;;
esac
""",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    return bin_dir, calls


def _script_env(tmp_path: Path, bin_dir: Path, calls: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "HCLOUD_TOKEN": "fixture-token",
            "HCLOUD_CALLS": str(calls),
            "PROJECT_ENV_FILE": str(tmp_path / "missing.env"),
            "TEST_DR_STATE_ROOT": str(tmp_path / "state"),
            "PATH": f"{bin_dir}:{env['PATH']}",
        }
    )
    return env


def test_dr_endpoint_script_is_executable_and_parses():
    assert SCRIPT.is_file()
    assert os.access(SCRIPT, os.X_OK)
    result = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_dr_endpoint_uses_owned_delete_protected_persistent_volume():
    content = SCRIPT.read_text(encoding="utf-8")
    for contract in (
        'VOLUME="${PROJECT}-data"',
        "--label managed-by=test-dr-endpoint",
        '--label "campaign=${CAMPAIGN}"',
        "--label purpose=dr-object-storage",
        "--label dr-initialization=pending",
        "--enable-protection delete",
        'hcloud volume attach "$VOLUME" --server "$PROJECT"',
        'jq -e \'.protection.delete == true\' ',
    ):
        assert contract in content

    remove_endpoint = content.split("remove_endpoint() {", 1)[1].split(
        'if [[ "$ACTION" == down ]]', 1
    )[0]
    assert "hcloud volume delete" not in remove_endpoint
    assert 'hcloud volume enable-protection "$VOLUME" delete' in remove_endpoint
    assert 'persistent volume ${VOLUME} was retained' in remove_endpoint


def test_existing_volume_is_never_reformatted_and_is_campaign_bound():
    content = SCRIPT.read_text(encoding="utf-8")
    remote = content.split("<<'REMOTE'", 1)[1].split("REMOTE", 1)[0]
    assert '[[ "$VOLUME_MAY_INITIALIZE" == true ]]' in remote
    assert "existing persistent DR volume has no recognized filesystem; refusing to format" in remote
    assert "new persistent DR volume contains an unexpected signature; refusing to format" in remote
    assert 'marker=/var/lib/test-dr-minio/.test-dr-campaign' in remote
    assert '[[ $(<"$marker") == "$CAMPAIGN" ]]' in remote
    assert remote.index('[[ "$VOLUME_MAY_INITIALIZE" == true ]]') < remote.index("mkfs.ext4")
    assert '.labels["dr-initialization"] == "pending"' in content
    assert 'volume add-label --overwrite "$VOLUME" dr-initialization=ready' in content


def test_purge_requires_exact_campaign_specific_phrase_before_mutation():
    content = SCRIPT.read_text(encoding="utf-8")
    purge = content.split('if [[ "$ACTION" == purge ]]', 1)[1].split(
        ': "${BACKUP_DR_ACCESS_KEY', 1
    )[0]
    confirmation = '[[ "$PURGE_CONFIRMATION" == "$expected_confirmation" ]]'
    assert 'expected_confirmation="PURGE ${CAMPAIGN} DR DATA"' in purge
    assert confirmation in purge
    assert purge.index(confirmation) < purge.index("remove_endpoint")
    assert purge.index(confirmation) < purge.index("hcloud volume disable-protection")
    assert purge.index("hcloud volume disable-protection") < purge.index("hcloud volume delete")
    assert "delete protection was restored when possible" in purge


def test_wrong_purge_phrase_performs_no_provider_calls(tmp_path):
    bin_dir, calls = _fake_hcloud(tmp_path)
    result = subprocess.run(
        [str(SCRIPT), "purge", "lab01", "wrong"],
        env=_script_env(tmp_path, bin_dir, calls),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "refusing destructive purge" in result.stderr
    assert not calls.exists()


def test_down_retains_and_verifies_protected_volume(tmp_path):
    bin_dir, calls = _fake_hcloud(tmp_path)
    result = subprocess.run(
        [str(SCRIPT), "down", "lab01"],
        env=_script_env(tmp_path, bin_dir, calls),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    provider_calls = calls.read_text(encoding="utf-8")
    assert "volume describe lab01-dr-data -o json" in provider_calls
    assert "volume delete" not in provider_calls
    assert "volume disable-protection" not in provider_calls
    assert "was retained with delete protection" in result.stdout


def test_credentials_are_not_logged_or_written_to_state():
    content = SCRIPT.read_text(encoding="utf-8")
    state = content.split('cat > "$STATE_FILE"', 1)[1].split("EOF", 1)[0]
    assert "BACKUP_DR_ACCESS_KEY" not in state
    assert "BACKUP_DR_SECRET_KEY" not in state
    assert "set -x" not in content


def test_recreated_server_removes_only_its_provider_assigned_stale_host_key():
    content = SCRIPT.read_text(encoding="utf-8")
    assigned = 'SERVER_IP=$(hcloud server describe "$PROJECT"'
    remove = 'ssh-keygen -R "$SERVER_IP" -f "$KNOWN_HOSTS_FILE"'
    first_ssh = 'ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new'
    assert assigned in content
    assert remove in content
    assert content.index(assigned) < content.index(remove) < content.index(first_ssh)
