from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/fun-games-edge-proxy"


def load_yaml(path):
    return yaml.safe_load(path.read_text())


def test_cutover_is_fail_closed_and_reversible():
    defaults = load_yaml(ROLE / "defaults/main.yml")
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert defaults["fun_games_edge_mode"] == "audit"
    assert defaults["fun_games_edge_confirm_cutover"] is False
    assert "s3.funfiesta.games" in defaults["fun_games_edge_required_hosts"]
    assert "fun_games_edge_confirm_cutover | bool" in tasks
    assert "difference(fun_games_edge_hosts | map(attribute='hostname') | list)" in tasks
    assert "Back up SafeLine configuration and certificates" in tasks
    assert "Restore SafeLine immediately" in tasks
    assert 'fun_games_edge_mode == "rollback"' in tasks


def test_s3_proxy_preserves_sigv4_and_uses_canonical_tls_sni():
    template = (ROLE / "templates/nginx.conf.j2").read_text()

    assert "proxy_set_header Host $host;" in template
    assert "proxy_ssl_server_name on;" in template
    assert "proxy_ssl_name" in template
    assert "proxy_request_buffering off;" in template
    assert "proxy_buffering off;" in template
    assert "client_max_body_size 0;" in template
    assert "resolver {{ fun_games_edge_resolvers | join(' ') }}" in template


def test_canary_is_loopback_only_and_runtime_is_hardened():
    template = (ROLE / "templates/compose.yaml.j2").read_text()

    assert "127.0.0.1:{{ fun_games_edge_canary_https_port }}:8443" in template
    assert "read_only: true" in template
    assert "no-new-privileges:true" in template
    assert "cap_drop:" in template
    assert "max-size: 20m" in template
