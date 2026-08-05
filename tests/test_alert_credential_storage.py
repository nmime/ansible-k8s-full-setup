import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/store-alert-credentials.py"


def test_alert_credentials_use_non_echoing_prompts_and_atomic_encryption():
    source = SCRIPT.read_text()
    tree = ast.parse(source)
    assert source.count("getpass.getpass") == 2
    assert "vault.encrypt" in source
    assert "os.replace" in source
    assert "alert_telegram_bot_token" in source
    assert "alert_telegram_chat_id" in source
    assert "--env-file" in source
    assert "--discover-chat-id" in source
    assert "ALERTS_TELEGRAM_BOT_TOKEN" in source
    assert "ALERTS_TELEGRAM_CHAT_ID" in source
    generator = (ROOT / "roles/generate-secrets/tasks/main.yml").read_text()
    assert "lookup('env', 'ALERTS_TELEGRAM_BOT_TOKEN')" in generator
    assert "lookup('env', 'ALERTS_TELEGRAM_CHAT_ID')" in generator
    assert "lookup('env', 'ALERT_TELEGRAM_BOT_TOKEN')" in generator
    assert "lookup('env', 'ALERT_TELEGRAM_CHAT_ID')" in generator
    assert "urlopen(request, timeout=15)" in source
    assert "len(chat_ids) != 1" in source
    assert "require_mode_0600(args.env_file" in source
    assert "bot_token}/getUpdates" in source
    assert "allowed_keys = set(BOT_TOKEN_ENV_KEYS + CHAT_ID_ENV_KEYS)" in source
    assert "if key not in allowed_keys:" in source
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "print"
        and any(
            isinstance(argument, ast.Name)
            and argument.id in {"bot_token", "chat_id"}
            for argument in node.args
        )
        for node in ast.walk(tree)
    )


def test_alert_credentials_never_put_secret_values_in_subprocess_arguments():
    source = SCRIPT.read_text()
    tree = ast.parse(source)

    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"run", "Popen", "call", "check_call", "check_output"}
        for node in ast.walk(tree)
    )
