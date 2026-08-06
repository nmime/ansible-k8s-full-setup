from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_priority_classes_keep_services_above_non_preempting_ci():
    defaults = yaml.safe_load((ROOT / "defaults/main.yml").read_text())
    classes = {item["name"]: item for item in defaults["workload_priority_classes"]}
    assert classes["n0xeid-platform-critical"]["value"] > classes[
        "n0xeid-production"
    ]["value"]
    assert classes["n0xeid-production"]["value"] > 0
    for name in ("n0xeid-ci", "n0xeid-batch"):
        assert classes[name]["value"] < 0
        assert classes[name]["preemption_policy"] == "Never"


def test_late_priority_reconcile_covers_controller_kinds_and_namespace_labels():
    tasks = (ROOT / "roles/workload-priority/tasks/main.yml").read_text()
    playbook = (ROOT / "playbooks/deploy_platform.yml").read_text()
    assert "kind: Deployment" in tasks
    assert "kind: StatefulSet" in tasks
    assert "kind: DaemonSet" in tasks
    assert "n0xeid.xyz/environment-tier" in tasks
    assert "priorityClassName" in tasks
    assert "name: workload-priority" in playbook


def test_ci_manager_override_is_not_promoted_to_platform_critical():
    defaults = yaml.safe_load((ROOT / "defaults/main.yml").read_text())
    assert defaults["workload_priority_controller_overrides"] == {
        "gitlab-ci-general/Deployment/gitlab-runner": "n0xeid-ci"
    }
    apply_task = (
        ROOT / "roles/workload-priority/tasks/apply-controller.yml"
    ).read_text()
    assert "workload_priority_controller_overrides.get" in apply_task


def test_operator_owned_statefulsets_are_patched_at_the_source():
    apply_task = (
        ROOT / "roles/workload-priority/tasks/apply-controller.yml"
    ).read_text()
    dragonfly_tasks = (ROOT / "roles/dragonfly/tasks/main.yml").read_text()
    assert "Resolve a Dragonfly operator owner" in apply_task
    assert "kind: Dragonfly" in apply_task
    assert "Wait for the Dragonfly operator projection" in apply_task
    assert "priorityClassName: \"{{ dragonfly_priority_class_name }}\"" in dragonfly_tasks
    database_tasks = (ROOT / "roles/k8s-databases/tasks/main.yml").read_text()
    assert "priorityClassName: '{{ mongodb_priority_class_name }}'" in database_tasks


def test_active_operations_can_defer_only_exact_controllers_and_finally_fail_closed():
    defaults = yaml.safe_load(
        (ROOT / "roles/workload-priority/defaults/main.yml").read_text()
    )
    tasks = (ROOT / "roles/workload-priority/tasks/main.yml").read_text()
    assert defaults["workload_priority_temporary_exclusions"] == []
    assert "namespace ~ '/' ~ item.kind ~ '/' ~ item.metadata.name" in tasks
    assert "difference(workload_priority_temporary_exclusions)" in tasks
    assert "Report explicitly deferred controllers" in tasks
    assert "from_json)['items']" in tasks
    assert "from_json).items" not in tasks
