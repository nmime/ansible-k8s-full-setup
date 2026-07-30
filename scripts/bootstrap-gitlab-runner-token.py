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
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse
import uuid


REPO_ROOT = Path(__file__).resolve().parents[1]
TOKEN_RE = re.compile(
    r"^glrt-(?=.{16,}$)[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*$"
)
SIMPLE_YAML_SCALAR_RE = re.compile(r"^[A-Za-z0-9._~-]+$")


class BootstrapError(RuntimeError):
    """Expected, safely reportable bootstrap failure."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def kubernetes_timestamp(value: datetime) -> str:
    # coordination.k8s.io Lease acquireTime/renewTime use metav1.MicroTime,
    # whose JSON decoder requires exactly six fractional-second digits.
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def parse_kubernetes_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise BootstrapError("bootstrap Lease has no valid renewal timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise BootstrapError("bootstrap Lease has an invalid renewal timestamp") from error
    if parsed.tzinfo is None:
        raise BootstrapError("bootstrap Lease renewal timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def run_command(
    argv: list[str],
    *,
    stdin: str | None = None,
    sensitive: bool = False,
    check: bool = True,
    label: str | None = None,
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
        stage = label or Path(argv[0]).name
        raise BootstrapError(f"{stage} failed: {detail or 'no diagnostic'}")
    return result


def require_secure_regular_file(path: Path, label: str) -> None:
    if path.is_symlink() or not path.is_file():
        raise BootstrapError(f"{label} must be a regular, non-symlink file: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        raise BootstrapError(f"{label} must have mode 0600 or stricter: {path}")


def env_assignment_re(env_name: str) -> re.Pattern[str]:
    return re.compile(
        rf"^(?:export\s+)?{re.escape(env_name)}=(?P<value>.*)$",
        re.MULTILINE,
    )


def yaml_key_re(yaml_key: str) -> re.Pattern[str]:
    escaped = re.escape(yaml_key)
    return re.compile(
        rf"^(?P<indent>[ \t]*)(?P<key>{escaped}|['\"]{escaped}['\"])[ \t]*:"
        r"(?P<raw>[^\n]*)$",
        re.MULTILINE,
    )


def parse_env_token(
    content: str, env_name: str = "GITLAB_RUNNER_TOKEN"
) -> str | None:
    match = env_assignment_re(env_name).search(content)
    if not match:
        return None
    value = match.group("value").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    return value or None


def inspect_yaml_token_assignment(
    content: str, yaml_key: str = "gitlab_runner_token"
) -> tuple[re.Match[str] | None, str | None]:
    matches = list(yaml_key_re(yaml_key).finditer(content))
    if len(matches) > 1:
        raise BootstrapError(
            f"encrypted platform secrets contain duplicate {yaml_key} keys"
        )
    if not matches:
        return None, None

    match = matches[0]
    if match.group("indent") or match.group("key") != yaml_key:
        raise BootstrapError(
            f"{yaml_key} must be one unquoted top-level scalar assignment"
        )
    raw = match.group("raw").rstrip("\r").strip()
    if not raw or raw in {"~", "null", "Null", "NULL"}:
        return match, None
    if raw[0] in "'\"":
        if len(raw) < 2 or raw[-1] != raw[0]:
            raise BootstrapError(f"{yaml_key} has an unterminated quoted scalar")
        value = raw[1:-1]
        if raw[0] in value or "\\" in value:
            raise BootstrapError(f"{yaml_key} uses unsupported YAML escaping")
        if value and not TOKEN_RE.fullmatch(value):
            raise BootstrapError(f"{yaml_key} contains an invalid scalar token")
        return match, value or None
    if not SIMPLE_YAML_SCALAR_RE.fullmatch(raw):
        raise BootstrapError(
            f"{yaml_key} must be a simple scalar, not a complex YAML value"
        )
    if not TOKEN_RE.fullmatch(raw):
        raise BootstrapError(f"{yaml_key} contains an invalid scalar token")
    return match, raw


def parse_yaml_token(
    content: str, yaml_key: str = "gitlab_runner_token"
) -> str | None:
    _, value = inspect_yaml_token_assignment(content, yaml_key)
    return value


def replace_env_token(
    content: str,
    token: str,
    env_name: str = "GITLAB_RUNNER_TOKEN",
) -> str:
    assignment = env_assignment_re(env_name)
    replacement = f"{env_name}='{token}'"
    if assignment.search(content):
        return assignment.sub(replacement, content, count=1)
    separator = "" if not content or content.endswith("\n") else "\n"
    return f"{content}{separator}{replacement}\n"


def replace_yaml_token(
    content: str,
    token: str,
    yaml_key: str = "gitlab_runner_token",
) -> str:
    if not TOKEN_RE.fullmatch(token):
        raise BootstrapError("refusing to persist an invalid GitLab Runner authentication token")
    replacement = f'{yaml_key}: "{token}"'
    match, _ = inspect_yaml_token_assignment(content, yaml_key)
    if match:
        updated = f"{content[:match.start()]}{replacement}{content[match.end():]}"
    else:
        separator = "" if not content or content.endswith("\n") else "\n"
        updated = f"{content}{separator}{replacement}\n"

    resulting_match, resulting_value = inspect_yaml_token_assignment(
        updated, yaml_key
    )
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


class KubernetesLeaseLock:
    """Cluster-wide, renewable serialization for runner bootstrap mutations."""

    def __init__(
        self,
        kubectl: list[str],
        *,
        name: str = "ansible-k8s-runner-bootstrap",
        duration_seconds: int = 900,
        renew_interval_seconds: int = 60,
    ) -> None:
        self.kubectl = kubectl
        self.name = name
        self.duration_seconds = duration_seconds
        self.renew_interval_seconds = renew_interval_seconds
        self.holder = f"runner-bootstrap-{uuid.uuid4()}"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._renew_error: BootstrapError | None = None

    def _manifest(self, *, resource_version: str | None = None, released: bool = False) -> dict:
        now = utc_now()
        metadata = {"name": self.name}
        if resource_version:
            metadata["resourceVersion"] = resource_version
        spec = {
            "leaseDurationSeconds": 1 if released else self.duration_seconds,
            "acquireTime": kubernetes_timestamp(now),
            "renewTime": kubernetes_timestamp(now),
        }
        if not released:
            spec["holderIdentity"] = self.holder
        return {
            "apiVersion": "coordination.k8s.io/v1",
            "kind": "Lease",
            "metadata": metadata,
            "spec": spec,
        }

    def _apply(self, action: str, manifest: dict, *, check: bool) -> subprocess.CompletedProcess[str]:
        return run_command(
            self.kubectl + [action, "--filename=-", "--output=json"],
            stdin=json.dumps(manifest, separators=(",", ":")),
            check=check,
            label=f"GitLab bootstrap Lease {action}",
        )

    def _get(self, *, allow_absent: bool = False) -> dict | None:
        argv = self.kubectl + ["get", "lease", self.name, "--output=json"]
        if allow_absent:
            argv.append("--ignore-not-found")
        result = run_command(
            argv,
            label="GitLab bootstrap Lease read",
        )
        if allow_absent and not result.stdout.strip():
            return None
        try:
            lease = json.loads(result.stdout)
            if lease["metadata"]["name"] != self.name:
                raise KeyError("name")
            return lease
        except (json.JSONDecodeError, KeyError, TypeError) as error:
            raise BootstrapError("GitLab bootstrap Lease returned invalid metadata") from error

    def _is_available(self, lease: dict) -> bool:
        spec = lease.get("spec", {})
        holder = spec.get("holderIdentity") or ""
        if not holder:
            return True
        try:
            duration = int(spec["leaseDurationSeconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise BootstrapError("bootstrap Lease has no valid duration") from error
        if duration < 1:
            raise BootstrapError("bootstrap Lease duration must be positive")
        renewed = parse_kubernetes_timestamp(spec.get("renewTime") or spec.get("acquireTime"))
        return renewed + timedelta(seconds=duration) <= utc_now()

    def acquire(self) -> None:
        acquired = False
        last_create_error = "no diagnostic"
        saw_existing_lease = False
        for attempt in range(4):
            created = self._apply("create", self._manifest(), check=False)
            if created.returncode == 0:
                acquired = True
                break
            last_create_error = created.stderr.strip() or "no diagnostic"
            lease = self._get(allow_absent=True)
            if lease is None:
                # The create failure was not AlreadyExists. Retry atomically in
                # case the apiserver transiently rejected or lost the request;
                # --ignore-not-found avoids locale-dependent stderr parsing.
                if attempt < 3:
                    time.sleep(0.2)
                continue
            saw_existing_lease = True
            if not self._is_available(lease):
                raise BootstrapError("another GitLab Runner bootstrap currently holds the Lease")
            resource_version = lease.get("metadata", {}).get("resourceVersion")
            if not isinstance(resource_version, str) or not resource_version:
                raise BootstrapError("bootstrap Lease has no resourceVersion for safe takeover")
            replaced = self._apply(
                "replace",
                self._manifest(resource_version=resource_version),
                check=False,
            )
            if replaced.returncode == 0:
                acquired = True
                break
        if not acquired:
            if saw_existing_lease:
                raise BootstrapError("could not atomically take over the stale bootstrap Lease")
            raise BootstrapError(
                f"could not atomically create the absent bootstrap Lease: {last_create_error}"
            )
        self.assert_held()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name="gitlab-runner-bootstrap-lease",
            daemon=True,
        )
        self._thread.start()

    def _renew_once(self) -> None:
        lease = self._get()
        if lease.get("spec", {}).get("holderIdentity") != self.holder:
            raise BootstrapError("GitLab bootstrap Lease ownership was lost")
        resource_version = lease.get("metadata", {}).get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            raise BootstrapError("bootstrap Lease has no resourceVersion for renewal")
        self._apply(
            "replace",
            self._manifest(resource_version=resource_version),
            check=True,
        )

    def _renew_loop(self) -> None:
        while not self._stop.wait(self.renew_interval_seconds):
            try:
                self._renew_once()
            except BootstrapError as error:
                self._renew_error = error
                self._stop.set()
                return

    def assert_held(self) -> None:
        if self._renew_error:
            raise BootstrapError(f"GitLab bootstrap Lease renewal failed: {self._renew_error}")
        lease = self._get()
        spec = lease.get("spec", {})
        if spec.get("holderIdentity") != self.holder:
            raise BootstrapError("GitLab bootstrap Lease ownership was lost")
        if self._is_available(lease):
            raise BootstrapError("GitLab bootstrap Lease expired before the protected operation")

    def release(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.renew_interval_seconds + 1))
        try:
            lease = self._get()
        except BootstrapError:
            return
        if lease.get("spec", {}).get("holderIdentity") != self.holder:
            return
        resource_version = lease.get("metadata", {}).get("resourceVersion")
        if not isinstance(resource_version, str) or not resource_version:
            return
        # Releasing by optimistic replace, rather than delete, can never remove a
        # successor that acquired the Lease after our final ownership read.
        self._apply(
            "replace",
            self._manifest(resource_version=resource_version, released=True),
            check=False,
        )

    def __enter__(self) -> "KubernetesLeaseLock":
        self.acquire()
        return self

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        self.release()


class Bootstrapper:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.runner_tags = getattr(args, "runner_tags", "kubernetes,docker,k8s")
        self.run_untagged = bool(getattr(args, "run_untagged", True))
        self.access_level = getattr(args, "access_level", "not_protected")
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
            ],
            label="GitLab Toolbox Pod discovery",
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

    def rails(
        self,
        pod: str,
        source: str,
        *,
        stage: str,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return run_command(
            self.kubectl + ["exec", "-i", pod, "--", "gitlab-rails", "runner", "-"],
            stdin=source,
            sensitive=True,
            check=check,
            label=stage,
        )

    def require_compatible_version(self, pod: str) -> str:
        result = self.rails(
            pod,
            "STDOUT.write(Gitlab::VERSION)\n",
            stage="GitLab version compatibility check",
        )
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
require 'json'
require 'net/http'
require 'uri'
uri = URI({json.dumps(self.args.gitlab_internal_url + '/api/v4/runners/verify')})
response = Net::HTTP.post_form(uri, {{'token' => {json.dumps(token)}, 'system_id' => 's_ansible_k8s_bootstrap'}})
STDOUT.write(JSON.generate({{'status' => response.code.to_i}}))
"""
        result = self.rails(
            pod,
            source,
            stage="GitLab Runner token verification",
        )
        try:
            status = int(json.loads(result.stdout)["status"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise BootstrapError("GitLab Runner token verification returned invalid metadata") from error
        if status == 200:
            return True
        if status == 403:
            return False
        raise BootstrapError(f"GitLab Runner token verification returned unexpected HTTP {status}")

    def recover_managed_runner_token(self, pod: str) -> str | None:
        source = f"""
require 'json'
runners = Ci::Runner.where(description: {json.dumps(self.args.runner_description)}).to_a
payload = {{'count' => runners.length}}
payload['token'] = runners.first.token if runners.length == 1
STDOUT.write(JSON.generate(payload))
"""
        result = self.rails(
            pod,
            source,
            stage="managed GitLab Runner recovery",
        )
        try:
            payload = json.loads(result.stdout)
            count = int(payload["count"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise BootstrapError("managed GitLab Runner recovery returned invalid metadata") from error
        if count > 1:
            raise BootstrapError(
                f"found {count} runners with managed description {self.args.runner_description!r}; "
                "refusing to choose one"
            )
        if count == 0:
            return None
        token = payload.get("token")
        if not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
            raise BootstrapError("the single managed GitLab Runner has no recoverable glrt token")
        return token

    def assert_bootstrap_pat_revoked(self, pod: str, pat_name: str) -> None:
        source = f"""
require 'json'
user = User.find_by_username('root') || raise('root user not found')
tokens = user.personal_access_tokens.where(name: {json.dumps(pat_name)}).to_a
payload = {{'count' => tokens.length}}
if tokens.length == 1
  payload['id'] = tokens.first.id
  payload['revoked'] = tokens.first.revoked?
end
STDOUT.write(JSON.generate(payload))
"""
        result = self.rails(
            pod,
            source,
            stage="bootstrap PAT revoked-state assertion",
        )
        try:
            payload = json.loads(result.stdout)
            count = int(payload["count"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise BootstrapError("bootstrap PAT revoked-state assertion returned invalid metadata") from error
        if count != 1:
            raise BootstrapError(
                f"expected exactly one bootstrap PAT for revoked-state proof, found {count}"
            )
        if payload.get("revoked") is not True:
            raise BootstrapError("the exact bootstrap PAT is not revoked")

    def revoke_bootstrap_pat(self, pod: str, pat_name: str) -> None:
        source = f"""
user = User.find_by_username('root') || raise('root user not found')
tokens = user.personal_access_tokens.where(name: {json.dumps(pat_name)}).to_a
raise('duplicate bootstrap PAT name') if tokens.length > 1
token = tokens.first
token.revoke! if token && !token.revoked?
"""
        result = self.rails(
            pod,
            source,
            stage="bootstrap PAT revocation",
            check=False,
        )
        try:
            self.assert_bootstrap_pat_revoked(pod, pat_name)
        except BootstrapError as error:
            if result.returncode != 0:
                raise BootstrapError(
                    "bootstrap PAT revocation exited nonzero and the exact PAT's revoked state "
                    "could not be proven"
                ) from error
            raise

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
        try:
            create_pat_result = self.rails(
                pod,
                create_pat,
                stage="bootstrap create_runner-scoped PAT creation",
                check=False,
            )
            if create_pat_result.returncode != 0:
                pat_state = f"""
user = User.find_by_username('root') || raise('root user not found')
tokens = user.personal_access_tokens.where(name: {json.dumps(pat_name)}).to_a
exit(tokens.length == 1 && !tokens.first.revoked? ? 0 : 4)
"""
                proof = self.rails(
                    pod,
                    pat_state,
                    stage="bootstrap PAT creation-state assertion",
                    check=False,
                )
                if proof.returncode != 0:
                    raise BootstrapError(
                        "bootstrap PAT creation exited nonzero and active state could not be proven"
                    )
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
  'tag_list' => {json.dumps(self.runner_tags)},
  'run_untagged' => {json.dumps(str(self.run_untagged).lower())},
  'access_level' => {json.dumps(self.access_level)},
  'paused' => 'false',
  'maintenance_note' => 'Managed by ansible-k8s-full-setup'
)
http = Net::HTTP.new(uri.hostname, uri.port)
http.use_ssl = uri.scheme == 'https'
response = http.request(request)
raise("runner API returned HTTP #{{response.code}}") unless response.code == '201'
runner_token = JSON.parse(response.body).fetch('token')
raise('runner API returned an invalid authentication token') unless runner_token.match?(/\\Aglrt-[A-Za-z0-9_-]+(?:\\.[A-Za-z0-9_-]+)*\\z/)
STDOUT.write(runner_token)
"""
            result = self.rails(
                pod,
                create_runner,
                stage="GitLab instance Runner API creation",
            )
            token = result.stdout.strip()
            if not TOKEN_RE.fullmatch(token):
                raise BootstrapError("GitLab did not return a valid runner authentication token")
            return token
        finally:
            self.revoke_bootstrap_pat(pod, pat_name)


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
        label="Ansible Vault platform-secret decryption",
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
            label="Ansible Vault platform-secret encryption",
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
    parser.add_argument(
        "--runner-kind",
        choices=("standard", "image-builder", "docker-host"),
        default="standard",
    )
    parser.add_argument("--runner-description")
    args = parser.parse_args()
    if args.runner_kind == "image-builder":
        args.runner_description = (
            args.runner_description or "ansible-k8s-protected-image-builder"
        )
        args.runner_tags = "image-build"
        args.run_untagged = False
        args.access_level = "ref_protected"
        args.token_env_name = "GITLAB_IMAGE_BUILDER_RUNNER_TOKEN"
        args.token_yaml_key = "gitlab_image_builder_runner_token"
    elif args.runner_kind == "docker-host":
        args.runner_description = (
            args.runner_description or "ansible-k8s-protected-docker-host"
        )
        args.runner_tags = "docker-host"
        args.run_untagged = False
        args.access_level = "ref_protected"
        args.token_env_name = "GITLAB_DOCKER_HOST_RUNNER_TOKEN"
        args.token_yaml_key = "gitlab_docker_host_runner_token"
    else:
        args.runner_description = (
            args.runner_description or "ansible-k8s-platform-runner"
        )
        args.runner_tags = "kubernetes,docker,k8s"
        args.run_untagged = True
        args.access_level = "not_protected"
        args.token_env_name = "GITLAB_RUNNER_TOKEN"
        args.token_yaml_key = "gitlab_runner_token"
    return args


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

        bootstrapper = Bootstrapper(args)
        pod = bootstrapper.toolbox_pod()
        version = bootstrapper.require_compatible_version(pod)
        with KubernetesLeaseLock(bootstrapper.kubectl) as bootstrap_lock:
            vault_plaintext = decrypt_secrets(args.secrets_file, args.vault_password_file)
            env_content = (
                args.env_file.read_text(encoding="utf-8") if args.env_file.exists() else ""
            )
            candidates = {
                token
                for token in (
                    parse_yaml_token(vault_plaintext, args.token_yaml_key),
                    parse_env_token(env_content, args.token_env_name),
                )
                if token and TOKEN_RE.fullmatch(token)
            }

            managed_token = bootstrapper.recover_managed_runner_token(pod)
            valid = {token for token in candidates if bootstrapper.verify_token(pod, token)}
            if managed_token:
                if not bootstrapper.verify_token(pod, managed_token):
                    raise BootstrapError(
                        "the single managed GitLab Runner token was recovered but failed live "
                        "verification"
                    )
                valid.add(managed_token)
            if len(valid) > 1:
                raise BootstrapError(
                    "local state and the managed GitLab Runner resolve to different valid tokens"
                )
            if valid:
                runner_token = next(iter(valid))
                action = (
                    "recovered and reused the managed live runner token"
                    if managed_token
                    else "reused the existing live runner token"
                )
            else:
                bootstrap_lock.assert_held()
                runner_token = bootstrapper.create_runner_token(pod)
                if not bootstrapper.verify_token(pod, runner_token):
                    raise BootstrapError("the newly created GitLab Runner token failed live verification")
                action = "created a new instance runner token"

            bootstrap_lock.assert_held()
            encrypt_secrets(
                args.secrets_file,
                args.vault_password_file,
                replace_yaml_token(
                    vault_plaintext, runner_token, args.token_yaml_key
                ),
            )
            atomic_write(
                args.env_file,
                replace_env_token(
                    env_content, runner_token, args.token_env_name
                ),
            )
            bootstrap_lock.assert_held()
        print(f"GitLab {version}: {action}; encrypted secrets and ignored .env are synchronized.")
        print("The authentication token was not printed or passed in a command argument.")
        return 0
    except BootstrapError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
