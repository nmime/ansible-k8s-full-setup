from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
ALERTING = REPO / "roles" / "k8s-observability" / "tasks" / "alerting.yml"
OBSERVABILITY_MAIN = (
    REPO / "roles" / "k8s-observability" / "tasks" / "main.yml"
)


def alerting_source() -> str:
    return ALERTING.read_text(encoding="utf-8")


def test_runner_manager_availability_uses_deployment_state_not_stale_scrapes():
    content = alerting_source()
    assert "kube_deployment_spec_replicas" in content
    assert "kube_deployment_status_replicas_available" in content
    assert "Intentional scale-to-zero is excluded" in content
    assert 'job=~".*gitlab.*runner.*"} == 0' not in content


def test_runner_error_and_queue_alerts_preserve_actionable_dimensions():
    content = alerting_source()
    assert "sum by (namespace, pod, job, level)" in content
    assert "sum by (namespace, pod, job, status)" in content
    assert "sum by (namespace, runner, runner_name, le)" in content
    assert "humanizeDuration $value" in content
    assert content.count("runbook_url: 'https://git.n0xeid.xyz/fun/argocd/") >= 4


def test_telegram_templates_render_runner_identity_when_available():
    content = alerting_source()
    assert content.count("Labels.namespace") >= 2
    assert content.count("Labels.pod") >= 2
    assert content.count("Labels.runner_name") >= 2
    assert content.count("Labels.system_id") >= 2
    assert content.count("Labels.level") >= 2
    assert content.count("Labels.status") >= 2


def test_alerting_supports_narrow_reconciliation():
    content = OBSERVABILITY_MAIN.read_text(encoding="utf-8")
    include = """- name: Include alerting (VMAlertmanager + VMAlert + VMRules + routing)\n  ansible.builtin.import_tasks: alerting.yml\n  tags: [alerting]"""
    assert include in content
