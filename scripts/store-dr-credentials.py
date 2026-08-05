#!/usr/bin/env python3
"""Atomically persist external DR credentials in an Ansible Vault file.

Plaintext exists only in process memory. Credential values are accepted through
non-echoing prompts and are never written to stdout, argv, or a temporary file.
"""

from __future__ import annotations

import argparse
import fcntl
import getpass
import json
import os
from pathlib import Path
import stat
import tempfile

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.parsing.vault import VaultLib, VaultSecret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--vault-password-file", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--credentials-fifo",
        type=Path,
        help=argparse.SUPPRESS,
    )
    return parser.parse_args()


def require_mode_0600(path: Path, label: str) -> None:
    mode = path.stat().st_mode & 0o777
    if mode != 0o600:
        raise SystemExit(f"{label} must have mode 0600: {path} has {mode:04o}")


def read_credentials_from_fifo(path: Path) -> tuple[str, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
        fifo_stat = os.fstat(stream.fileno())
        if not stat.S_ISFIFO(fifo_stat.st_mode):
            raise SystemExit("credentials input must be a named pipe")
        if fifo_stat.st_uid != os.getuid():
            raise SystemExit("credentials named pipe must be owned by this user")
        if fifo_stat.st_mode & 0o777 != 0o600:
            raise SystemExit("credentials named pipe must have mode 0600")
        payload = json.load(stream)
    if not isinstance(payload, dict):
        raise SystemExit("credentials named pipe must contain one JSON mapping")
    return str(payload.get("access_key", "")).strip(), str(
        payload.get("secret_key", "")
    ).strip()


def main() -> None:
    args = parse_args()
    if not args.file.is_file():
        raise SystemExit(f"encrypted secrets file does not exist: {args.file}")
    if not args.vault_password_file.is_file():
        raise SystemExit(
            f"Ansible Vault password file does not exist: {args.vault_password_file}"
        )
    require_mode_0600(args.file, "encrypted secrets file")
    require_mode_0600(args.vault_password_file, "Ansible Vault password file")

    if args.credentials_fifo:
        access_key, secret_key = read_credentials_from_fifo(args.credentials_fifo)
    else:
        access_key = getpass.getpass("Hetzner S3 Access Key: ").strip()
        secret_key = getpass.getpass("Hetzner S3 Secret Key: ").strip()
    if len(access_key) < 8 or len(secret_key) < 16:
        raise SystemExit("credential lengths are invalid; encrypted file was not changed")

    vault_password = args.vault_password_file.read_bytes().strip()
    vault = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(vault_password))])
    lock_path = args.file.with_suffix(args.file.suffix + ".lock")
    lock_path.touch(mode=0o600, exist_ok=True)

    with lock_path.open("r+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        encrypted = args.file.read_bytes()
        if not encrypted.startswith(b"$ANSIBLE_VAULT;"):
            raise SystemExit("refusing to modify a non-Ansible-Vault secrets file")
        current = yaml.safe_load(vault.decrypt(encrypted)) or {}
        if not isinstance(current, dict):
            raise SystemExit("encrypted secrets file must contain one YAML mapping")

        current.update(
            {
                "backup_dr_access_key": access_key,
                "backup_dr_secret_key": secret_key,
                "backup_dr_endpoint": args.endpoint,
                "backup_dr_region": args.region,
                "backup_dr_bucket": args.bucket,
            }
        )
        plaintext = yaml.safe_dump(current, sort_keys=False).encode()
        replacement = vault.encrypt(plaintext)

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
        finally:
            if os.path.exists(staged_name):
                os.unlink(staged_name)

    print(f"Encrypted DR credentials stored in {args.file}; values were not printed.")


if __name__ == "__main__":
    main()
