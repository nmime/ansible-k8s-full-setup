from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vault_audit_claim_follows_compact_profile_storage_by_default():
    normalize = (ROOT / "playbooks/tasks/normalize_profile.yml").read_text()
    tasks = (ROOT / "roles/k8s-secrets/tasks/main.yml").read_text()

    assert "vault_audit_storage_size:" in normalize
    assert "secrets.vault.audit_storage_size" in normalize
    assert "secrets.vault.storage_size | default(vault_storage_size)" in normalize
    assert "vault_audit_storage:" in tasks
    assert "size: '{{ vault_audit_storage }}'" in tasks
    assert "size: 20Gi" not in tasks
