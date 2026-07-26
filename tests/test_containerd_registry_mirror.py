from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/k8s-cluster-management/tasks/main.yml"


def test_private_registry_mirror_is_resolvable_from_cluster_nodes():
    content = TASKS.read_text()

    assert "host: https://registry.{{ domain }}" in content
    assert "host: http://gitlab-registry.gitlab.svc.cluster.local" not in content
