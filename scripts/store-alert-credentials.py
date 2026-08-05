#!/usr/bin/env python3
"""Atomically store Telegram alert credentials in encrypted recovery state.

Values are accepted through non-echoing prompts or a mode-0600 dotenv file and
never enter argv, stdout, or a plaintext temporary file. When only a bot token
is present, the destination can be discovered only when Telegram has exactly
one chat update for that bot. Run the secrets and alerting reconciles after this
helper so the governed Vault mirror and Alertmanager receive the values.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shlex
import tempfile
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.parsing.vault import VaultLib, VaultSecret


BOT_TOKEN = re.compile(r"^[0-9]{6,}:[A-Za-z0-9_-]{20,}$")
CHAT_ID = re.compile(r"^-?[0-9]+$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
BOT_TOKEN_ENV_KEYS = (
    "ALERTS_TELEGRAM_BOT_TOKEN",
    "ALERT_TELEGRAM_BOT_TOKEN",
)
CHAT_ID_ENV_KEYS = (
    "ALERTS_TELEGRAM_CHAT_ID",
    "ALERT_TELEGRAM_CHAT_ID",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--vault-password-file", required=True, type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--discover-chat-id", action="store_true")
    return parser.parse_args()


def require_mode_0600(path: Path, label: str) -> None:
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise SystemExit(f"{label} must have mode 0600: {path} has {mode:04o}")


def read_dotenv(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    allowed_keys = set(BOT_TOKEN_ENV_KEYS + CHAT_ID_ENV_KEYS)
    for line_number, raw_line in enumerate(path.read_text().splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, encoded_value = line.partition("=")
        key = key.strip()
        if key not in allowed_keys:
            continue
        if not separator or not ENV_KEY.fullmatch(key):
            raise SystemExit(f"invalid dotenv entry at {path}:{line_number}")
        try:
            parsed = shlex.split(encoded_value, comments=True, posix=True)
        except ValueError as error:
            raise SystemExit(
                f"invalid dotenv quoting at {path}:{line_number}"
            ) from error
        if len(parsed) > 1:
            raise SystemExit(f"dotenv value must be a single token at {path}:{line_number}")
        if key in values:
            raise SystemExit(f"duplicate dotenv key at {path}:{line_number}: {key}")
        values[key] = parsed[0] if parsed else ""
    return values


def first_value(values: dict[str, str], keys: tuple[str, ...]) -> str:
    return next((values[key].strip() for key in keys if values.get(key)), "")


def discover_chat_id(bot_token: str) -> str:
    request = Request(
        f"https://api.telegram.org/bot{bot_token}/getUpdates",
        data=b"limit=100&timeout=0",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as error:
        raise SystemExit(
            "Telegram API validation failed; token and URL were not printed"
        ) from error
    if payload.get("ok") is not True or not isinstance(payload.get("result"), list):
        raise SystemExit("Telegram rejected getUpdates; no credential was stored")

    chat_ids: set[str] = set()
    for update in payload["result"]:
        if not isinstance(update, dict):
            continue
        for key in (
            "message",
            "edited_message",
            "channel_post",
            "edited_channel_post",
            "my_chat_member",
            "chat_member",
            "chat_join_request",
        ):
            event = update.get(key)
            chat = event.get("chat") if isinstance(event, dict) else None
            candidate = chat.get("id") if isinstance(chat, dict) else None
            if isinstance(candidate, int) and candidate != 0:
                chat_ids.add(str(candidate))
    if not chat_ids:
        raise SystemExit(
            "Telegram has no chat update for this bot; send /start to the bot "
            "or add it to the target group, then run again"
        )
    if len(chat_ids) != 1:
        raise SystemExit(
            "Telegram returned multiple destination chats; set "
            "ALERTS_TELEGRAM_CHAT_ID explicitly in the mode-0600 env file"
        )
    return next(iter(chat_ids))


def main() -> None:
    args = parse_args()
    for path, label in (
        (args.file, "encrypted secrets file"),
        (args.vault_password_file, "Ansible Vault password file"),
    ):
        if not path.is_file() or path.is_symlink():
            raise SystemExit(f"{label} must be a regular file: {path}")
        require_mode_0600(path, label)

    if args.env_file:
        if not args.env_file.is_file() or args.env_file.is_symlink():
            raise SystemExit(
                f"dotenv credentials file must be a regular file: {args.env_file}"
            )
        require_mode_0600(args.env_file, "dotenv credentials file")
        dotenv = read_dotenv(args.env_file)
        bot_token = first_value(dotenv, BOT_TOKEN_ENV_KEYS)
        chat_id = first_value(dotenv, CHAT_ID_ENV_KEYS)
        if not chat_id and args.discover_chat_id and BOT_TOKEN.fullmatch(bot_token):
            chat_id = discover_chat_id(bot_token)
    else:
        bot_token = getpass.getpass("Telegram BotFather token: ").strip()
        chat_id = getpass.getpass("Telegram target chat ID: ").strip()
    if not BOT_TOKEN.fullmatch(bot_token):
        raise SystemExit("Telegram bot token format is invalid; no file was changed")
    if not CHAT_ID.fullmatch(chat_id) or int(chat_id) == 0:
        raise SystemExit("Telegram chat ID is invalid; no file was changed")

    vault = VaultLib(
        [
            (
                DEFAULT_VAULT_ID_MATCH,
                VaultSecret(args.vault_password_file.read_bytes().strip()),
            )
        ]
    )
    lock_path = args.file.with_suffix(args.file.suffix + ".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)

    with lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        encrypted = args.file.read_bytes()
        if not encrypted.startswith(b"$ANSIBLE_VAULT;"):
            raise SystemExit("refusing to modify a non-Ansible-Vault secrets file")
        current = yaml.safe_load(vault.decrypt(encrypted)) or {}
        if not isinstance(current, dict):
            raise SystemExit("encrypted secrets file must contain one YAML mapping")
        current["alert_telegram_bot_token"] = bot_token
        current["alert_telegram_chat_id"] = chat_id
        replacement = vault.encrypt(
            yaml.safe_dump(current, sort_keys=False).encode()
        )

        fd, staged_name = tempfile.mkstemp(
            prefix=f".{args.file.name}.", suffix=".encrypted", dir=args.file.parent
        )
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb") as staged:
                staged.write(replacement)
                staged.flush()
                os.fsync(staged.fileno())
            os.replace(staged_name, args.file)
            os.chmod(args.file, 0o600)
        finally:
            if os.path.exists(staged_name):
                os.unlink(staged_name)

    print("Encrypted Telegram credentials stored; values were not printed.")


if __name__ == "__main__":
    main()
