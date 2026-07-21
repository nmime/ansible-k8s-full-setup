from pathlib import Path

from ansible.parsing.dataloader import DataLoader


ROOT = Path(__file__).resolve().parents[1]
TASK_ROOTS = (ROOT / "roles", ROOT / "playbooks" / "tasks")
CHILD_TASK_KEYS = ("tasks", "pre_tasks", "post_tasks", "handlers", "block", "rescue", "always")
LOADER = DataLoader()


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
