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
    assert content.count("runbook_url: '{{ alert_runbook_url_resolved }}'") >= 4
    assert "git.platform.example.com" not in content
    assert "github.com/nmime/ansible-k8s-full-setup/blob/main/docs/RUNBOOK.md" in content


def test_telegram_templates_are_compact_and_render_context_when_available():
    content = alerting_source()
    assert content.count("Labels.namespace") >= 2
    assert content.count("Labels.pod") >= 2
    assert content.count("Labels.runner_name") >= 2
    assert content.count("Labels.level") >= 2
    assert content.count("Labels.status") >= 2
    assert content.count("<b>Context:</b>") == 2
    assert content.count('📘 <a href="{{ .Annotations.runbook_url }}">Runbook</a>') == 2
    assert "Namespace: <code>" not in content
    assert "Deployment: <code>" not in content
    assert "System ID: <code>" not in content


def test_job_failures_only_alert_when_the_job_does_not_recover():
    content = alerting_source()
    assert "max by (namespace, job_name) (kube_job_status_failed > 0)" in content
    assert "unless on (namespace, job_name)" in content
    assert "max by (namespace, job_name) (kube_job_status_succeeded > 0)" in content
    assert "for: 5m" in content


def test_prometheus_printf_templates_do_not_render_escaped_quotes():
    content = alerting_source()
    assert content.count("{{ '{{ printf \"%.0f\" $value }}' }}") == 3
    assert 'printf \\\"%.0f\\\"' not in content


def test_alerting_supports_narrow_reconciliation():
    content = OBSERVABILITY_MAIN.read_text(encoding="utf-8")
    include = """- name: Include alerting (VMAlertmanager + VMAlert + VMRules + routing)\n  ansible.builtin.import_tasks: alerting.yml\n  tags: [alerting]"""
    assert include in content
