#!/usr/bin/env python3
"""Run a command with external DR credentials from encrypted recovery state."""

from __future__ import annotations

import argparse
import fcntl
import os
from pathlib import Path
import subprocess
import sys

import yaml
from ansible.constants import DEFAULT_VAULT_ID_MATCH
from ansible.parsing.vault import VaultLib, VaultSecret


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True, type=Path)
    parser.add_argument("--vault-password-file", required=True, type=Path)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    return args


def main() -> None:
    args = parse_args()
    lock_path = args.file.with_suffix(args.file.suffix + ".lock")
    lock_path.touch(mode=0o600, exist_ok=True)
    os.chmod(lock_path, 0o600)

    with lock_path.open("r+") as lock_file:
        # Hold one project-scoped writer lock for the entire reconcile. The
        # child may atomically replace args.file while this lock remains valid.
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        password = args.vault_password_file.read_bytes().strip()
        vault = VaultLib([(DEFAULT_VAULT_ID_MATCH, VaultSecret(password))])
        saved = yaml.safe_load(vault.decrypt(args.file.read_bytes())) or {}
        if not isinstance(saved, dict):
            raise SystemExit("encrypted secrets file must contain one YAML mapping")

        access_key = str(saved.get("backup_dr_access_key", ""))
        secret_key = str(saved.get("backup_dr_secret_key", ""))
        environment = os.environ.copy()
        if not environment.get("BACKUP_DR_ACCESS_KEY"):
            environment["BACKUP_DR_ACCESS_KEY"] = access_key
        if not environment.get("BACKUP_DR_SECRET_KEY"):
            environment["BACKUP_DR_SECRET_KEY"] = secret_key
        if len(environment["BACKUP_DR_ACCESS_KEY"]) < 8:
            raise SystemExit("encrypted recovery state has no valid DR access key")
        if len(environment["BACKUP_DR_SECRET_KEY"]) < 16:
            raise SystemExit("encrypted recovery state has no valid DR secret key")
        if not environment.get("AWS_ACCESS_KEY_ID"):
            environment["AWS_ACCESS_KEY_ID"] = environment["BACKUP_DR_ACCESS_KEY"]
        if not environment.get("AWS_SECRET_ACCESS_KEY"):
            environment["AWS_SECRET_ACCESS_KEY"] = environment["BACKUP_DR_SECRET_KEY"]

        completed = subprocess.run(args.command, env=environment, check=False)
    raise SystemExit(completed.returncode)


if __name__ == "__main__":
    main()
