from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_controller_manager_terminal_pod_gc_is_bounded():
    defaults = (ROOT / "defaults/main.yml").read_text()
    tasks = (ROOT / "roles/k8s-cluster-management/tasks/main.yml").read_text()
    profile = yaml.safe_load(
        (ROOT / "platform-orchestrator/profiles/medium-optimized.yaml").read_text()
    )

    assert "kube_controller_terminated_pod_gc_threshold:" in defaults
    assert "terminated_pod_gc_threshold | default(1)" in defaults
    assert profile["kubernetes"]["terminated_pod_gc_threshold"] == 1
    assert (
        "kube_controller_terminated_pod_gc_threshold: "
        "{{ kube_controller_terminated_pod_gc_threshold | int }}"
    ) in tasks
