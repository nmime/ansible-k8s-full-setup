from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
ORCHESTRATOR = ROOT / "platform-orchestrator" / "platform.sh"


def test_gitlab_runner_deploy_uses_the_narrow_runner_tag():
    content = ORCHESTRATOR.read_text(encoding="utf-8")

    assert (
        'gitlab-runner) require_component_enabled "$component"; '
        "run_playbook --tags gitlab-runner" in content
    )
    assert (
        'gitlab-runner) require_component_enabled "$component"; '
        "run_playbook --tags gitlab " not in content
    )
