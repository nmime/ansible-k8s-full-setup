from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_helm4_reclaims_operator_replaced_vault_statefulset_fields():
    tasks = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()

    read = tasks.index("Read Vault StatefulSet field ownership before Helm 4 reconcile")
    reclaim = tasks.index(
        "Return operator-replaced Vault StatefulSet fields to Helm ownership"
    )
    install = tasks.index("Install Vault via Helm")
    assert read < reclaim < install

    block = tasks[reclaim:install]
    assert "kubectl-replace" in block
    assert "apply --server-side --force-conflicts --field-manager=helm" in block
    assert "del(.metadata.creationTimestamp" in block
    assert "retries: 3" in block
