from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

FORBIDDEN_LITERALS = (
    "n0xeid",
    "dadya",
    "durak",
    "fun-games",
    "fun_games",
    "social-agents",
    "social_agents",
    "funfiesta.games",
    "git.n0xeid.xyz",
    "vault.n0xeid.xyz",
    "argocd.n0xeid.xyz",
    "65.109.236.184",
    "65.109.247.139",
    "95.217.170.241",
)

SECRET_ASSIGNMENT = re.compile(
    r"(?m)^\s*(?:HCLOUD_TOKEN|VAULT_TOKEN|GITLAB_TOKEN|TELEGRAM_BOT_TOKEN)"
    r"\s*=\s*(?P<value>[^\s#]*)"
)
PRIVATE_KEY = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")


def tracked_files() -> list[Path]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT, text=False
    )
    return [ROOT / item.decode() for item in output.rstrip(b"\0").split(b"\0")]


def test_public_tree_contains_no_cluster_identity_or_secret_material() -> None:
    failures: list[str] = []
    for path in tracked_files():
        if path == Path(__file__) or not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue

        lowered = content.lower()
        for literal in FORBIDDEN_LITERALS:
            if literal.lower() in lowered:
                failures.append(f"{path.relative_to(ROOT)}: forbidden {literal}")
        for match in SECRET_ASSIGNMENT.finditer(content):
            value = match.group("value").strip("'\"")
            if value and not value.startswith(("$", "...", "<", "example", "replace", "changeme")):
                failures.append(
                    f"{path.relative_to(ROOT)}: populated secret assignment"
                )
        if PRIVATE_KEY.search(content):
            failures.append(f"{path.relative_to(ROOT)}: private key material")

    assert not failures, "\n".join(failures)
