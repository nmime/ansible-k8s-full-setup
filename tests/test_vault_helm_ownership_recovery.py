from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_helm4_reclaims_operator_replaced_vault_statefulset_fields():
    tasks = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()

    read = tasks.index("Read Vault StatefulSet field ownership before Helm 4 reconcile")
    reclaim = tasks.index(
        "Clear inconsistent Vault field ownership before Helm 4 reconcile"
    )
    install = tasks.index("Install Vault via Helm")
    assert read < reclaim < install

    block = tasks[reclaim:install]
    assert "kubectl-replace" in block
    assert "kubectl patch statefulset vault" in block
    assert "managedFields" in block
    assert "[{}]" in block
    assert "the StatefulSet spec and pods are not modified" in block
    assert "retries: 3" in block
