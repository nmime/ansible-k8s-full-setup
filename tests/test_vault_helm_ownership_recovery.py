from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_helm4_forces_desired_vault_field_ownership_natively():
    tasks = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()

    install = tasks.index("Install Vault via Helm")
    block = tasks[install : tasks.index("Wait for Vault pods", install)]
    assert "force_conflicts: '{{ vault_helm_major | int >= 4 }}'" in block
    assert "kubectl patch statefulset vault" not in tasks
    assert "managedFields" not in block
