from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
RECONCILE = ROOT / "roles/k8s-secrets/tasks/reconcile.yml"


def _tasks() -> list[dict]:
    return yaml.safe_load(RECONCILE.read_text(encoding="utf-8"))


def _task_by_name(tasks: list[dict], name: str) -> dict:
    return next(task for task in tasks if task.get("name") == name)


def test_raft_quorum_proof_runs_after_unseal_with_leader_credentials():
    tasks = _tasks()
    names = [task.get("name") for task in tasks]

    assert names.index("Unseal raft peers (HA mode)") < names.index(
        "Read Vault raft membership from the leader"
    )
    assert names.index("Copy the transient Vault token file into the Vault pod") < names.index(
        "Read Vault raft membership from the leader"
    )
    assert names.index("Prove exact Vault raft quorum") < names.index(
        "Discover Vault secrets engines before reconciliation"
    )

    read = _task_by_name(tasks, "Read Vault raft membership from the leader")
    execution = read["kubernetes.core.k8s_exec"]
    assert execution["pod"] == "vault-0"
    assert 'VAULT_TOKEN=$(cat "$1")' in execution["command"]
    assert "vault operator raft list-peers -format=json" in execution["command"]
    assert "vault_token_remote_file" in execution["command"]
    assert read["changed_when"] is False
    assert read["no_log"] is True
    assert read["retries"] == 12
    assert read["delay"] == 5
    assert "get('servers', [])" in read["until"]
    assert "vault_replicas | int" in read["until"]


def test_raft_quorum_proof_requires_exact_nodes_voters_and_one_leader():
    tasks = _tasks()
    expected = _task_by_name(tasks, "Build expected Vault raft membership")
    facts = expected["ansible.builtin.set_fact"]
    assert "range(0, vault_replicas | int)" in facts["vault_expected_raft_node_ids"]
    assert "regex_replace" in facts["vault_expected_raft_node_ids"]
    assert "vault-" in facts["vault_expected_raft_node_ids"]
    assert "get('servers', [])" in facts["vault_raft_servers"]

    proof = _task_by_name(tasks, "Prove exact Vault raft quorum")
    conditions = " ".join(proof["ansible.builtin.assert"]["that"])
    assert "vault_raft_servers | length" in conditions
    assert "map(attribute='node_id')" in conditions
    assert "vault_expected_raft_node_ids | sort" in conditions
    assert "selectattr('voter', 'equalto', true)" in conditions
    assert "selectattr('leader', 'equalto', true)" in conditions
    assert "| length) == 1" in conditions
    assert proof["when"] == "vault_ha | bool and vault_init_data is defined"
