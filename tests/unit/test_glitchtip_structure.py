from pathlib import Path
from urllib.parse import unquote, urlsplit

from jinja2 import Environment
import yaml


ROOT = Path(__file__).resolve().parents[2]
TASKS = (ROOT / "roles/glitchtip/tasks/main.yml").read_text(encoding="utf-8")
DEFAULTS = (ROOT / "roles/glitchtip/defaults/main.yml").read_text(encoding="utf-8")


def glitchtip_config_templates():
    tasks = yaml.safe_load(TASKS)
    task = next(item for item in tasks if item["name"] == "Create GlitchTip config secret")
    return task["kubernetes.core.k8s"]["definition"]["stringData"]


def test_glitchtip_authenticates_to_external_dragonfly():
    assert "name: dragonfly-auth" in TASKS
    assert "gt_dragonfly_secret.resources[0].data.password | b64decode" in TASKS
    assert "gt_dragonfly_password | urlencode" in TASKS
    assert "REDIS_URL: \"redis://:" in TASKS


def test_glitchtip_connection_urls_encode_every_userinfo_delimiter():
    templates = glitchtip_config_templates()
    password = "colon:@ slash/ query? hash# percent% angle< space "
    values = {
        "glitchtip_db_user": "glitch/tip",
        "gt_pg_password": password,
        "gt_pg_host_direct": "postgres.databases.svc.cluster.local",
        "glitchtip_db_name": "glitch/tip",
        "gt_dragonfly_password": password,
        "glitchtip_redis_host": "dragonfly.dragonfly.svc.cluster.local",
        "glitchtip_redis_port": 6379,
        "glitchtip_redis_db": 0,
    }
    environment = Environment(autoescape=False)

    database_url = environment.from_string(templates["DATABASE_URL"]).render(values)
    database = urlsplit(database_url)
    assert database.hostname == values["gt_pg_host_direct"]
    assert database.port == 5432
    assert unquote(database.username) == values["glitchtip_db_user"]
    assert unquote(database.password) == password
    assert unquote(database.path.removeprefix("/")) == values["glitchtip_db_name"]

    redis_url = environment.from_string(templates["REDIS_URL"]).render(values)
    redis = urlsplit(redis_url)
    assert redis.hostname == values["glitchtip_redis_host"]
    assert redis.port == values["glitchtip_redis_port"]
    assert unquote(redis.password) == password


def test_glitchtip_does_not_log_external_service_passwords():
    password_fact = TASKS.split(
        "- name: Set GlitchTip Dragonfly password fact", 1
    )[1].split("- name:", 1)[0]
    config_secret = TASKS.split("- name: Create GlitchTip config secret", 1)[1].split(
        "- name:", 1
    )[0]
    assert "no_log: true" in password_fact
    assert "no_log: true" in config_secret


def test_glitchtip_recovers_only_exact_stale_pending_helm_release():
    assert "glitchtip_helm_pending_stale_seconds: 3600" in DEFAULTS
    assert "(glitchtip_helm_pending_stale_seconds | int) >= 900" in TASKS
    assert "owner=helm,name=glitchtip" in TASKS
    assert "custom-columns=NAME:.metadata.name" in TASKS
    assert "sh\\.helm\\.release\\.v1\\.glitchtip" in TASKS
    assert "(gt_helm_pending_records | length) == 1" in TASKS
    assert "is not the newest release revision" in TASKS
    assert "metadata changed after inspection" in TASKS
    assert "preconditions:" in TASKS
    assert 'uid: "{{ gt_helm_pending_records[0].uid }}"' in TASKS
    assert 'resourceVersion: "{{ gt_helm_pending_records[0].resource_version }}"' in TASKS
    secret_delete = TASKS.split(
        "- name: Delete only the UID-matched stale GlitchTip Helm revision", 1
    )[1].split("- name:", 1)[0]
    assert "kind: Secret" in secret_delete
    assert "no_log: true" in secret_delete


def test_glitchtip_stale_recovery_preserves_external_data_and_avoids_hooks():
    recovery = TASKS.split("- name: Recover a stale GlitchTip Helm transaction", 1)[1]
    recovery = recovery.split("- name: Install GlitchTip via Helm", 1)[0]
    assert "helm\n          - uninstall\n          - glitchtip" in recovery
    assert "--namespace" in recovery
    assert "--no-hooks" in recovery
    assert "postgres" not in recovery.lower()
    assert "dragonfly" not in recovery.lower()
    assert "persistentvolumeclaim" not in recovery.lower()


def test_glitchtip_recent_or_ambiguous_pending_transaction_fails_closed():
    assert "Refuse to interrupt a recent GlitchTip Helm transaction" in TASKS
    assert "Refusing recovery until it is at least" in TASKS
    assert "transaction is ambiguous" in TASKS
