#!/usr/bin/env python3
"""Bootstrap a GitLab Runner authentication token without exposing it.

The helper uses a short-lived, create_runner-scoped root PAT created through
the GitLab Rails runner, calls the supported POST /user/runners API from the
toolbox Pod, revokes the PAT, and atomically persists the returned glrt- token.
Secret values are sent only through process stdin and captured pipes; they are
never command arguments or terminal output.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
from urllib.parse import urlparse


REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(r"^glrt-[A-Za-z0-9_-]{16,}$")
ENV_ASSIGNMENT_RE = re.compile(
    r"^(?:export\s+)?GITLAB_RUNNER_TOKEN=(?P<value>.*)$", re.MULTILINE
)
YAML_KEY_RE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<key>gitlab_runner_token|['\"]gitlab_runner_token['\"])[ \t]*:"
    r"(?P<raw>[^\n]*)$",
    re.MULTILINE,
)
SIMPLE_YAML_SCALAR_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class BootstrapError(RuntimeError):
    """Expected, safely reportable bootstrap failure."""


def run_command(
    argv: list[str],
    *,
    stdin: str | None = None,
    sensitive: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        argv,
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = "sensitive command output suppressed" if sensitive else result.stderr.strip()
        raise BootstrapError(f"{Path(argv[0]).name} failed: {detail or 'no diagnostic'}")
    return result


def require_secure_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"{label} must be a regular, non-symlink file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BootstrapError(f"{label} must have mode 0600 or stricter: {path}")


def parse_env_token(content: str) -> str | None:
    match = ENV_ASSIGNMENT_RE.search(content)
    if not match:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value or None


def inspect_yaml_token_assignment(content: str) -> tuple[re.Match[str] | None, str | None]:
    matches = list(YAML_KEY_RE.finditer(content))
    if len(matches) > 1:
        raise BootstrapError("encrypted platform secrets contain duplicate gitlab_runner_token keys")
    if not matches:
        return None, None

    match = matches[0]
    if match.group("indent") or match.group("key") != "gitlab_runner_token":
        raise BootstrapError(
            "gitlab_runner_token must be one unquoted top-level scalar assignment"
        )
    raw = match.group("raw").rstrip("\r").strip()
    if not raw or raw in {"~", "null", "Null", "NULL"}:
        return match, None
    if raw[0] in "'\"":
        if len(raw) < 2 or raw[-1] != raw[0]:
            raise BootstrapError("gitlab_runner_token has an unterminated quoted scalar")
        value = raw[1:-1]
        if raw[0] in value or "\\" in value:
            raise BootstrapError("gitlab_runner_token uses unsupported YAML escaping")
        if value and not TOKEN_RE.fullmatch(value):
            raise BootstrapError("gitlab_runner_token contains an invalid scalar token")
        return match, value or None
    if not SIMPLE_YAML_SCALAR_RE.fullmatch(raw):
        raise BootstrapError("gitlab_runner_token must be a simple scalar, not a complex YAML value")
    if not TOKEN_RE.fullmatch(raw):
        raise BootstrapError("gitlab_runner_token contains an invalid scalar token")
    return match, raw


def parse_yaml_token(content: str) -> str | None:
    _, value = inspect_yaml_token_assignment(content)
    return value


def replace_env_token(content: str, token: str) -> str:
    replacement = f"GITLAB_RUNNER_TOKEN='{token}'"
    if ENV_ASSIGNMENT_RE.search(content):
        return ENV_ASSIGNMENT_RE.sub(replacement, content, count=1)
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{replacement}\n"


def replace_yaml_token(content: str, token: str) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise BootstrapError("refusing to persist an invalid GitLab Runner authentication token")
    replacement = f'gitlab_runner_token: "{token}"'
    match, _ = inspect_yaml_token_assignment(content)
    if match:
        updated = f"{content[:match.start()]}{replacement}{content[match.end():]}"
    else:
        separator = "" if not content or content.endswith("\n") else "\n"
        updated = f"{content}{separator}{replacement}\n"

    resulting_match, resulting_value = inspect_yaml_token_assignment(updated)
    if resulting_match is None or resulting_value != token:
        raise BootstrapError("failed to produce exactly one GitLab Runner token scalar")
    return updated


def atomic_write(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


class Bootstrapper:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.kubectl = ["kubectl", "--kubeconfig", str(args.kubeconfig), "-n", args.namespace]

    def toolbox_pod(self) -> str:
        result = run_command(
            self.kubectl
            + [
                "get",
                "pods",
                "--selector=release=gitlab,app=toolbox",
                "--field-selector=status.phase=Running",
                "--output=json",
            ]
        )
        try:
            pods = json.loads(result.stdout)["items"]
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise BootstrapError("kubectl returned invalid toolbox Pod inventory") from error
        ready = [
            pod
            for pod in pods
            if any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])
            )
        ]
        if len(ready) != 1:
            raise BootstrapError(f"expected exactly one Ready GitLab toolbox Pod, found {len(ready)}")
        return ready[0]["metadata"]["name"]

    def rails(self, pod: str, source: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        return run_command(
            self.kubectl + ["exec", "-i", pod, "--", "gitlab-rails", "runner", "-"],
            stdin=source,
            sensitive=True,
            check=check,
        )

    def require_compatible_version(self, pod: str) -> str:
        result = self.rails(pod, "STDOUT.write(Gitlab::VERSION)\n")
        version = result.stdout.strip()
        match = re.fullmatch(r"(\d+)\.(\d+)(?:\.\d+)?(?:[-+].*)?", version)
        if not match:
            raise BootstrapError("GitLab returned an unrecognized version")
        major, minor = (int(value) for value in match.groups())
        if (major, minor) < (17, 1) or major > 19:
            raise BootstrapError(
                f"GitLab {major}.{minor} is outside the validated 17.1-19.x bootstrap range"
            )
        return version

    def verify_token(self, pod: str, token: str) -> bool:
        source = f"""
require 'net/http'
require 'uri'
uri = URI({json.dumps(self.args.gitlab_internal_url + '/api/v4/runners/verify')})
response = Net::HTTP.post_form(uri, {{'token' => {json.dumps(token)}, 'system_id' => 's_ansible_k8s_bootstrap'}})
exit(response.code == '200' ? 0 : 3)
"""
        return self.rails(pod, source, check=False).returncode == 0

    def create_runner_token(self, pod: str) -> str:
        pat = "glpat-" + secrets.token_urlsafe(32)
        pat_name = "ansible-runner-bootstrap-" + secrets.token_hex(8)
        create_pat = f"""
user = User.find_by_username('root') || raise('root user not found')
token = user.personal_access_tokens.create(
  scopes: ['create_runner'],
  name: {json.dumps(pat_name)},
  expires_at: 1.day.from_now
)
token.set_token({json.dumps(pat)})
token.save!
"""
        self.rails(pod, create_pat)
        try:
            create_runner = f"""
require 'json'
require 'net/http'
require 'uri'
uri = URI({json.dumps(self.args.gitlab_internal_url + '/api/v4/user/runners')})
request = Net::HTTP::Post.new(uri)
request['PRIVATE-TOKEN'] = {json.dumps(pat)}
request.set_form_data(
  'runner_type' => 'instance_type',
  'description' => {json.dumps(self.args.runner_description)},
  'tag_list' => 'kubernetes,docker,k8s',
  'run_untagged' => 'true',
  'paused' => 'false',
  'maintenance_note' => 'Managed by ansible-k8s-full-setup'
)
http = Net::HTTP.new(uri.hostname, uri.port)
http.use_ssl = uri.scheme == 'https'
response = http.request(request)
raise("runner API returned HTTP #{{response.code}}") unless response.code == '201'
runner_token = JSON.parse(response.body).fetch('token')
raise('runner API returned an invalid authentication token') unless runner_token.match?(/\\Aglrt-[A-Za-z0-9_-]+\\z/)
STDOUT.write(runner_token)
"""
            result = self.rails(pod, create_runner)
            token = result.stdout.strip()
            if not TOKEN_RE.fullmatch(token):
                raise BootstrapError("GitLab did not return a valid runner authentication token")
            return token
        finally:
            revoke_pat = f"""
token = PersonalAccessToken.find_by_token({json.dumps(pat)})
token.revoke! if token
"""
            self.rails(pod, revoke_pat)


def decrypt_secrets(path: Path, password_file: Path) -> str:
    result = run_command(
        [
            "ansible-vault",
            "view",
            "--vault-password-file",
            str(password_file),
            str(path),
        ],
        sensitive=True,
    )
    return result.stdout


def encrypt_secrets(path: Path, password_file: Path, plaintext: str) -> None:
    with tempfile.TemporaryDirectory(prefix=f".{path.name}.", dir=path.parent) as directory:
        os.chmod(directory, 0o700)
        plain_path = Path(directory) / "secrets.yml"
        encrypted_path = Path(directory) / "secrets.yml.vault"
        plain_path.write_text(plaintext, encoding="utf-8")
        os.chmod(plain_path, 0o600)
        run_command(
            [
                "ansible-vault",
                "encrypt",
                "--encrypt-vault-id",
                "default",
                "--vault-password-file",
                str(password_file),
                "--output",
                str(encrypted_path),
                str(plain_path),
            ],
            sensitive=True,
        )
        os.chmod(encrypted_path, 0o600)
        os.replace(encrypted_path, path)
        os.chmod(path, 0o600)


def require_ignored_env(path: Path) -> None:
    try:
        relative = path.resolve().relative_to(REPO_ROOT)
    except ValueError as error:
        raise BootstrapError(f"environment file must be inside the repository: {path}") from error
    result = run_command(
        ["git", "-C", str(REPO_ROOT), "check-ignore", "--quiet", "--", str(relative)],
        check=False,
    )
    if result.returncode != 0:
        raise BootstrapError(f"refusing to write a Git-tracked environment file: {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kubeconfig",
        type=Path,
        default=Path(os.environ.get("KUBECONFIG", "~/.kube/config")).expanduser(),
    )
    parser.add_argument("--namespace", default="gitlab")
    parser.add_argument("--env-file", type=Path, default=REPO_ROOT / ".env")
    parser.add_argument(
        "--secrets-file", type=Path, default=REPO_ROOT / "playbooks/.platform-secrets.yml"
    )
    parser.add_argument(
        "--vault-password-file",
        type=Path,
        default=(
            Path(os.environ["ANSIBLE_VAULT_PASSWORD_FILE"]).expanduser()
            if os.environ.get("ANSIBLE_VAULT_PASSWORD_FILE")
            else None
        ),
    )
    parser.add_argument(
        "--gitlab-internal-url",
        default="http://gitlab-webservice-default.gitlab.svc.cluster.local:8181",
    )
    parser.add_argument("--runner-description", default="ansible-k8s-platform-runner")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        require_secure_regular_file(args.kubeconfig, "kubeconfig")
        if args.vault_password_file is None:
            raise BootstrapError(
                "--vault-password-file or ANSIBLE_VAULT_PASSWORD_FILE is required"
            )
        require_secure_regular_file(args.vault_password_file, "Ansible Vault password file")
        require_secure_regular_file(args.secrets_file, "encrypted platform secrets file")
        require_ignored_env(args.env_file)
        if args.env_file.exists():
            require_secure_regular_file(args.env_file, "repository environment file")
        parsed_url = urlparse(args.gitlab_internal_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
            raise BootstrapError("--gitlab-internal-url must be an HTTP(S) URL")

        vault_plaintext = decrypt_secrets(args.secrets_file, args.vault_password_file)
        env_content = args.env_file.read_text(encoding="utf-8") if args.env_file.exists() else ""
        candidates = {
            token
            for token in (parse_yaml_token(vault_plaintext), parse_env_token(env_content))
            if token and TOKEN_RE.fullmatch(token)
        }

        bootstrapper = Bootstrapper(args)
        pod = bootstrapper.toolbox_pod()
        version = bootstrapper.require_compatible_version(pod)
        valid = [token for token in candidates if bootstrapper.verify_token(pod, token)]
        if len(valid) > 1:
            raise BootstrapError(
                "the encrypted secrets file and .env contain different valid runner tokens"
            )
        if valid:
            runner_token = valid[0]
            action = "reused the existing live runner token"
        else:
            runner_token = bootstrapper.create_runner_token(pod)
            action = "created a new instance runner token"

        encrypt_secrets(
            args.secrets_file,
            args.vault_password_file,
            replace_yaml_token(vault_plaintext, runner_token),
        )
        atomic_write(args.env_file, replace_env_token(env_content, runner_token))
        print(f"GitLab {version}: {action}; encrypted secrets and ignored .env are synchronized.")
        print("The authentication token was not printed or passed in a command argument.")
        return 0
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
