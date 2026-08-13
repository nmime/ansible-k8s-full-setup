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
    assert defaults["fun_games_edge_acme_enabled"] is True
    assert defaults["fun_games_edge_required_hosts"] == [
        "uno.games.example.com",
        "api.uno.games.example.com",
        "backend.uno.games.example.com",
        "bot.uno.games.example.com",
        "admin.uno.games.example.com",
        "uno.pp.games.example.com",
        "api.uno.pp.games.example.com",
        "backend.uno.pp.games.example.com",
        "bot.uno.pp.games.example.com",
        "admin.uno.pp.games.example.com",
        "durak.games.example.com",
        "api.durak.games.example.com",
        "backend.durak.games.example.com",
        "bot.durak.games.example.com",
        "admin.durak.games.example.com",
        "durak.pp.games.example.com",
        "api.durak.pp.games.example.com",
        "backend.durak.pp.games.example.com",
        "bot.durak.pp.games.example.com",
        "admin.durak.pp.games.example.com",
        "s3.games.example.com",
    ]
    assert "fun_games_edge_confirm_cutover | bool" in tasks
    assert "difference(fun_games_edge_hosts | map(attribute='hostname') | list)" in tasks
    assert "Back up SafeLine configuration and certificates" in tasks
    assert "Encrypt the SafeLine archive with age" in tasks
    assert "Download encrypted SafeLine backup for round-trip verification" in tasks
    assert "Require exact DR backup round-trip" in tasks
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
    assert "proxy_set_header X-Forwarded-For $remote_addr;" in template
    assert "$proxy_add_x_forwarded_for" not in template
    assert "ssl_reject_handshake on;" in template
    assert "location ^~ /.well-known/acme-challenge/" in template
    assert "try_files $uri @origin_acme;" in template
    assert "location @origin_acme" in template


def test_canary_is_loopback_only_and_runtime_is_hardened():
    template = (ROLE / "templates/compose.yaml.j2").read_text()

    assert "127.0.0.1:{{ fun_games_edge_canary_https_port }}:8443" in template
    assert "read_only: true" in template
    assert "no-new-privileges:true" in template
    assert "cap_drop:" in template
    assert "group_add:" in template
    assert '"{{ fun_games_edge_tls_group_gid }}"' in template
    assert "{{ fun_games_edge_acme_webroot }}:/var/www/acme:ro" in template
    assert "cpus:" in template
    assert "mem_limit:" in template
    assert "pids_limit:" in template
    assert "max-size: 20m" in template


def test_canary_and_cutover_verify_real_upstream_responses():
    tasks = (ROLE / "tasks/main.yml").read_text()

    assert "Verify every canary upstream response" in tasks
    assert "Verify every production upstream response" in tasks
    assert "item.health_path | default('/')" in tasks
    assert "item.health_statuses" in tasks


def test_edge_certificates_are_least_privilege_and_automatically_renewed():
    tasks = (ROLE / "tasks/main.yml").read_text()
    deploy = (ROLE / "templates/deploy-certificate.sh.j2").read_text()
    hook = (ROLE / "templates/certbot-deploy-hook.sh.j2").read_text()

    assert '["cutover", "certificates"]' in tasks
    assert "symmetric_difference(_fun_games_edge_acme_domains)" in tasks
    assert "fun_games_edge_tls_group_name" in tasks
    assert 'mode: "0640"' in tasks
    assert "--no-random-sleep-on-renew" in tasks
    assert "name: certbot.timer" in tasks
    assert "exec -T proxy nginx -t" in deploy
    assert "exec -T proxy nginx -s reload" in deploy
    assert "Refusing unknown edge certificate" in deploy
    assert "RENEWED_LINEAGE" in hook


def test_bootstrap_certificates_are_validated_without_controller_files():
    playbook = (
        ROOT / "playbooks/fun-games-edge-stage-certificates.yml"
    ).read_text()

    assert "kubernetes.core.k8s_info" in playbook
    assert "delegate_to: localhost" in playbook
    assert "-checkend" in playbook
    assert "-checkhost" in playbook
    assert "[openssl, x509, -pubkey, -noout]" in playbook
    assert "[openssl, pkey, -pubout]" in playbook
    assert 'mode: "0600"' in playbook
    assert playbook.count("no_log: true") >= 7


def test_edge_access_uses_a_dedicated_key_and_guarded_root_lockout():
    playbook = (
        ROOT / "playbooks/fun-games-edge-bootstrap-access.yml"
    ).read_text()

    assert "FUN_GAMES_EDGE_ADMIN_PUBLIC_KEY" in playbook
    assert "password_lock: true" in playbook
    assert 'mode: "0600"' in playbook
    assert "validate: /usr/sbin/visudo -cf %s" in playbook
    assert "fun_games_edge_disable_root_ssh: false" in playbook
    assert "Require verified dedicated access before disabling root SSH" in playbook
    assert "validate: /usr/sbin/sshd -t -f %s" in playbook
