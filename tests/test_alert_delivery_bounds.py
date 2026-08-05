from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_telegram_html_groups_are_bounded_before_transport_truncation():
    tasks = (ROOT / "roles/k8s-observability/tasks/alerting.yml").read_text()

    assert "{{ range $index, $alert := .Alerts -}}" in tasks
    assert "{{ if lt $index 6 }}" in tasks
    assert "{{ if gt (len .Alerts) 6 }}Additional alerts are grouped" in tasks
    assert "{{ range .Alerts -}}" not in tasks
