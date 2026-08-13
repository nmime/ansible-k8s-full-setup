from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_medium_profile_has_unique_semantic_provider_volume_names() -> None:
    profile = yaml.safe_load((ROOT / 'platform-orchestrator/profiles/medium-optimized.yaml').read_text())
    names = profile['storage']['provider_volume_names']
    assert names
    assert len(set(names.values())) == len(names)
    assert all('/' in claim for claim in names)
    assert all(not name.startswith('pvc-') for name in names.values())


def test_volume_reconcile_is_idempotent_and_uses_csi_volume_ids() -> None:
    playbook = (ROOT / 'playbooks/reconcile_hcloud_volume_names.yml').read_text()
    task = (ROOT / 'playbooks/tasks/reconcile_hcloud_volume_name.yml').read_text()
    assert 'provider_volume_name_map | dict2items' in playbook
    assert "spec.csi.driver == 'csi.hetzner.cloud'" in task
    assert 'spec.csi.volumeHandle' in task
    assert 'provider_volume_description.stdout | from_json' in task
    assert 'hcloud\n      - volume\n      - update' in task
    assert 'delete' not in task
