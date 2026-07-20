from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TASKS = (ROOT / "roles/glitchtip/tasks/main.yml").read_text(encoding="utf-8")


def test_glitchtip_authenticates_to_external_dragonfly():
    assert "name: dragonfly-auth" in TASKS
    assert "gt_dragonfly_secret.resources[0].data.password | b64decode" in TASKS
    assert "gt_dragonfly_password | urlencode" in TASKS
    assert "REDIS_URL: \"redis://:" in TASKS


def test_glitchtip_does_not_log_external_service_passwords():
    password_fact = TASKS.split(
        "- name: Set GlitchTip Dragonfly password fact", 1
    )[1].split("- name:", 1)[0]
    config_secret = TASKS.split("- name: Create GlitchTip config secret", 1)[1].split(
        "- name:", 1
    )[0]
    assert "no_log: true" in password_fact
    assert "no_log: true" in config_secret
