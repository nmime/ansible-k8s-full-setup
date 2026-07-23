import re
from pathlib import Path

from ansible.parsing.dataloader import DataLoader


ROOT = Path(__file__).resolve().parents[1]
TASK_ROOTS = (ROOT / "roles", ROOT / "playbooks" / "tasks")
CHILD_TASK_KEYS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")
LOADER = DataLoader()
JINJA_EXPRESSION_RE = re.compile(r"{{(.*?)}}", re.DOTALL)
JINJA_IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")
HELM_SECRET_VARIABLE_SUFFIXES = (
    "_password",
    "_secret_key",
    "_access_key",
    "_runner_token",
    "_api_key",
    "_root_token",
    "_private_key",
    "_client_secret",
    "_auth_token",
)


def walk_tasks(value):
    if isinstance(value, list):
        for item in value:
            yield from walk_tasks(item)
        return
    if not isinstance(value, dict):
        return

    if "name" in value:
        yield value
    for key in CHILD_TASK_KEYS:
        yield from walk_tasks(value.get(key))


def contains_secret_payload(value):
    if isinstance(value, dict):
        if value.get("kind") == "Secret" or "stringData" in value:
            return True
        return any(contains_secret_payload(child) for child in value.values())
    if isinstance(value, list):
        return any(contains_secret_payload(child) for child in value)
    return False


def task_directly_operates_on_secret(task):
    for module, arguments in task.items():
        if module.rsplit(".", 1)[-1] not in {"k8s", "k8s_info"}:
            continue
        if contains_secret_payload(arguments):
            return True
    return False


def helm_interpolated_secret_variables(task):
    secret_variables = set()
    for module, arguments in task.items():
        if module.rsplit(".", 1)[-1] != "helm":
            continue
        for expression in JINJA_EXPRESSION_RE.findall(str(arguments)):
            for identifier in JINJA_IDENTIFIER_RE.findall(expression):
                if identifier.lower().endswith(HELM_SECRET_VARIABLE_SUFFIXES):
                    secret_variables.add(identifier)
    return secret_variables


def test_direct_kubernetes_secret_operations_are_always_censored():
    uncensored = []
    for task_root in TASK_ROOTS:
        for path in task_root.rglob("*.yml"):
            if task_root.name == "roles" and "tasks" not in path.relative_to(task_root).parts:
                continue
            document = LOADER.load_from_file(str(path))
            for task in walk_tasks(document):
                if task_directly_operates_on_secret(task) and task.get("no_log") is not True:
                    uncensored.append(
                        f"{path.relative_to(ROOT)}: {task.get('name', '<unnamed>')}"
                    )

    assert not uncensored, "uncensored Secret tasks:\n" + "\n".join(uncensored)


def test_helm_tasks_interpolating_secret_variables_are_always_censored():
    detected = set()
    uncensored = []
    for task_root in TASK_ROOTS:
        for path in task_root.rglob("*.yml"):
            if task_root.name == "roles" and "tasks" not in path.relative_to(task_root).parts:
                continue
            document = LOADER.load_from_file(str(path))
            for task in walk_tasks(document):
                secret_variables = helm_interpolated_secret_variables(task)
                if not secret_variables:
                    continue
                task_location = (
                    str(path.relative_to(ROOT)),
                    task.get("name", "<unnamed>"),
                )
                detected.add(task_location)
                if task.get("no_log") is not True:
                    uncensored.append(
                        f"{task_location[0]}: {task_location[1]} "
                        f"({', '.join(sorted(secret_variables))})"
                    )

    expected = {
        ("roles/glitchtip/tasks/main.yml", "Install GlitchTip via Helm"),
        ("roles/gitlab-selfhosted/tasks/main.yml", "Install GitLab Runner with Helm"),
        ("roles/k8s-observability/tasks/main.yml", "Install Grafana with Helm"),
        ("roles/k8s-observability/tasks/main.yml", "Install Loki with Helm"),
    }
    assert expected <= detected
    assert not uncensored, "uncensored Helm secret values:\n" + "\n".join(uncensored)


def test_vault_root_token_uses_transient_files_and_never_exec_argv():
    entrypoint = ROOT / "roles" / "k8s-secrets" / "tasks" / "main.yml"
    reconcile = ROOT / "roles" / "k8s-secrets" / "tasks" / "reconcile.yml"
    entrypoint_content = entrypoint.read_text(encoding="utf-8")
    reconcile_content = reconcile.read_text(encoding="utf-8")

    assert reconcile_content.count("vault_init_data.root_token") == 1
    assert 'content: "{{ vault_init_data.root_token }}\\n"' in reconcile_content
    assert "kubernetes.core.k8s_cp:" in reconcile_content
    assert "local_path: '{{ vault_token_controller_file }}'" in reconcile_content
    assert "remote_path: '{{ vault_token_remote_file }}'" in reconcile_content
    assert "mode: '0600'" in reconcile_content
    assert "chmod 0600 -- {{ vault_token_remote_file }}" in reconcile_content

    document = LOADER.load_from_file(str(reconcile))
    for task in walk_tasks(document):
        arguments = task.get("kubernetes.core.k8s_exec")
        if arguments is not None:
            assert "vault_init_data.root_token" not in str(arguments)

    assert "always:" in entrypoint_content
    assert "Remove the transient Vault token file from the pod" in entrypoint_content
    assert "rm -f -- {{ vault_token_remote_file }}" in entrypoint_content
    assert "Remove the transient Vault token file from the controller" in entrypoint_content
    assert "path: '{{ vault_token_controller_file }}'" in entrypoint_content
    assert entrypoint_content.count("no_log: true") >= 2


def test_persisted_grafana_admin_password_reconciliation_is_censored():
    path = ROOT / "roles" / "k8s-observability" / "tasks" / "main.yml"
    document = LOADER.load_from_file(str(path))
    task = next(
        item
        for item in walk_tasks(document)
        if item.get("name") == "Reconcile the persisted Grafana admin password"
    )
    assert task.get("no_log") is True
    command = task["ansible.builtin.shell"]
    assert "GRAFANA_ADMIN_PASSWORD" in command
    assert "--config /etc/grafana/grafana.ini" in command
    assert "http://localhost:3000/login" not in command

    verify = next(
        item
        for item in walk_tasks(document)
        if item.get("name") == "Verify the reconciled Grafana admin login"
    )
    assert verify.get("no_log") is True
    verify_command = verify["ansible.builtin.shell"]
    assert "http://localhost:3000/login" in verify_command
    assert '--arg password "$GRAFANA_ADMIN_PASSWORD"' not in verify_command
    assert "printf '%s' \"$GRAFANA_ADMIN_PASSWORD\"" in verify_command
    assert "input as $password" in verify_command
    assert verify["retries"] == 10
    assert verify["delay"] == 3
    assert verify["until"] == "_grafana_password_verification is succeeded"
    assert "reset-admin-password" in command
