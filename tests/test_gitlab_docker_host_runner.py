from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "playbooks" / "gitlab_docker_host_runner.yml"


def test_systemd_service_does_not_request_a_second_user_switch() -> None:
    playbook = PLAYBOOK.read_text(encoding="utf-8")
    exec_start = next(
        line.strip()
        for line in playbook.splitlines()
        if line.strip().startswith("ExecStart=/usr/local/bin/gitlab-runner run")
    )

    assert "User=gitlab-runner" in playbook
    assert "Group=gitlab-runner" in playbook
    assert "--user" not in exec_start
    assert "--service gitlab-runner" in exec_start


def _tasks() -> dict[str, dict]:
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    return {
        task["name"]: task
        for task in plays[0]["tasks"]
        if isinstance(task, dict) and "name" in task
    }


def test_runner_login_shell_has_no_console_cleanup_side_effect() -> None:
    tasks = _tasks()
    logout = tasks["Keep non-interactive runner login shells side-effect free"][
        "ansible.builtin.copy"
    ]

    assert logout["dest"] == "/var/lib/gitlab-runner/.bash_logout"
    assert "clear_console" not in logout["content"]
    assert logout["content"].strip().endswith("true")

    verify = tasks["Verify the runner login-shell lifecycle"]
    assert verify["become_user"] == "gitlab-runner"
    assert verify["environment"]["HOME"] == "/var/lib/gitlab-runner"
    assert "/bin/bash --login" in verify["ansible.builtin.shell"]["cmd"]


def test_runner_provisions_package_manager_shim_before_read_only_jobs() -> None:
    shim = _tasks()["Publish the pnpm Corepack shim for the restricted runner"][
        "ansible.builtin.command"
    ]

    assert shim["cmd"] == "corepack enable --install-directory /usr/local/bin pnpm"
    assert shim["creates"] == "/usr/local/bin/pnpm"
