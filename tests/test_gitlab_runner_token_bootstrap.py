"""Contracts for the GitLab Runner token bootstrap helper."""

import argparse
import base64
from importlib.util import module_from_spec, spec_from_file_location
import json
import os
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap-gitlab-runner-token.py"
SPEC = spec_from_file_location("gitlab_runner_bootstrap", SCRIPT)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def runner_token(value):
    """Build synthetic Runner tokens without hardcoded secret-shaped literals."""
    return "".join(("g", "lrt-", value))


DOTTED_TOKEN = runner_token(
    "abcdefghijklmnop.qrstuvwxyzABCDEF.GHIJKLMNOPQRSTUVW"
)
assert len(DOTTED_TOKEN) == 56 and DOTTED_TOKEN.count(".") == 2


def bootstrapper():
    args = argparse.Namespace(
        kubeconfig=Path("/secure/kubeconfig"),
        namespace="gitlab",
        gitlab_internal_url="http://gitlab-webservice-default.gitlab.svc:8181",
        runner_description="ansible-k8s-platform-runner",
    )
    return MODULE.Bootstrapper(args)


def lease_document(holder, *, resource_version="1", expired=False):
    renewed = MODULE.utc_now() - MODULE.timedelta(minutes=20) if expired else MODULE.utc_now()
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {
            "name": "ansible-k8s-runner-bootstrap",
            "resourceVersion": resource_version,
        },
        "spec": {
            "holderIdentity": holder,
            "leaseDurationSeconds": 900,
            "renewTime": MODULE.kubernetes_timestamp(renewed),
        },
    }


def test_lease_timestamps_use_kubernetes_microtime_wire_format():
    moment = MODULE.datetime(
        2026,
        7,
        22,
        8,
        42,
        9,
        tzinfo=MODULE.timezone.utc,
    )
    encoded = MODULE.kubernetes_timestamp(moment)
    assert encoded == "2026-07-22T08:42:09.000000Z"
    assert encoded != "2026-07-22T08:42:09Z"
    assert MODULE.parse_kubernetes_timestamp(encoded) == moment


def test_local_recovery_state_lock_is_project_scoped_and_mode_0600(tmp_path):
    secrets_file = tmp_path / ".platform-secrets.yml"
    lock = MODULE.LocalSecretsFileLock(secrets_file)
    with lock:
        assert lock.path == tmp_path / ".platform-secrets.yml.lock"
        assert lock.handle is not None
        assert os.stat(lock.path).st_mode & 0o777 == 0o600
    assert lock.handle is None


def test_lease_microtime_parser_preserves_fractional_seconds_and_offsets():
    encoded = "2026-07-22T13:42:09.123456+05:00"
    parsed = MODULE.parse_kubernetes_timestamp(encoded)
    assert parsed == MODULE.datetime(
        2026,
        7,
        22,
        8,
        42,
        9,
        123456,
        tzinfo=MODULE.timezone.utc,
    )


def test_every_lease_manifest_timestamp_is_six_digit_microtime():
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"])
    for manifest in (lock._manifest(), lock._manifest(resource_version="7", released=True)):
        for field in ("acquireTime", "renewTime"):
            value = manifest["spec"][field]
            assert MODULE.re.fullmatch(
                r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z", value
            )


def test_env_assignment_is_added_and_replaced_without_duplicates():
    token = runner_token("abcdefghijklmnopqrstuvwxyz")
    assert MODULE.replace_env_token("HCLOUD_TOKEN=x\n", token).endswith(
        f"GITLAB_RUNNER_TOKEN='{token}'\n"
    )
    updated = MODULE.replace_env_token(
        f"GITLAB_RUNNER_TOKEN='{runner_token('oldoldoldoldoldold')}'\nKEEP=yes\n",
        token,
    )
    assert updated.count("GITLAB_RUNNER_TOKEN=") == 1
    assert MODULE.parse_env_token(updated) == token


def test_plaintext_env_sync_is_opt_in(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "argv", [str(SCRIPT)])
    assert MODULE.parse_args().sync_env is False
    monkeypatch.setattr(MODULE.sys, "argv", [str(SCRIPT), "--sync-env"])
    assert MODULE.parse_args().sync_env is True


def test_runtime_secret_adoption_is_explicit_and_has_scoped_defaults(monkeypatch):
    monkeypatch.setattr(MODULE.sys, "argv", [str(SCRIPT)])
    args = MODULE.parse_args()
    assert args.adopt_runtime_secret is False
    assert args.runtime_secret_namespace == "gitlab"
    assert args.runtime_secret_name == "platform-gitlab-runner-auth"
    assert args.runtime_secret_key == "runner-token"

    monkeypatch.setattr(
        MODULE.sys,
        "argv",
        [str(SCRIPT), "--runner-kind", "image-builder", "--adopt-runtime-secret"],
    )
    args = MODULE.parse_args()
    assert args.adopt_runtime_secret is True
    assert args.runtime_secret_namespace == "gitlab-image-builds"
    assert args.runtime_secret_name == "platform-gitlab-image-builder-runner-auth"


def test_runtime_secret_token_is_read_from_exact_object_without_secret_in_argv(monkeypatch):
    token = DOTTED_TOKEN
    observed = {}

    def run(argv, **kwargs):
        observed["argv"] = argv
        observed["kwargs"] = kwargs
        payload = {
            "metadata": {"name": "runner-auth", "namespace": "gitlab"},
            "data": {"runner-token": base64.b64encode(token.encode()).decode()},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(MODULE, "run_command", run)
    assert MODULE.read_runtime_secret_token(
        Path("/secure/kubeconfig"), "gitlab", "runner-auth", "runner-token"
    ) == token
    assert token not in " ".join(observed["argv"])
    assert observed["kwargs"]["sensitive"] is True


def test_runtime_secret_adoption_canonicalizes_one_terminal_line_ending(monkeypatch):
    token = DOTTED_TOKEN
    documents = iter((token + "\n", token + "\r\n"))

    def run(argv, **_kwargs):
        value = next(documents)
        payload = {
            "metadata": {"name": "runner-auth", "namespace": "gitlab"},
            "data": {"runner-token": base64.b64encode(value.encode()).decode()},
        }
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    monkeypatch.setattr(MODULE, "run_command", run)
    for _line_ending in range(2):
        assert MODULE.read_runtime_secret_token(
            Path("/secure/kubeconfig"), "gitlab", "runner-auth", "runner-token"
        ) == token


def test_runtime_secret_token_rejects_wrong_object_and_noncanonical_value(monkeypatch):
    documents = iter(
        (
            {
                "metadata": {"name": "other", "namespace": "gitlab"},
                "data": {
                    "runner-token": base64.b64encode(DOTTED_TOKEN.encode()).decode()
                },
            },
            {
                "metadata": {"name": "runner-auth", "namespace": "gitlab"},
                "data": {
                    "runner-token": base64.b64encode(b"legacy-token").decode()
                },
            },
        )
    )

    monkeypatch.setattr(
        MODULE,
        "run_command",
        lambda argv, **_kwargs: subprocess.CompletedProcess(
            argv, 0, json.dumps(next(documents)), ""
        ),
    )
    for expected in ("invalid metadata", "no canonical glrt token"):
        try:
            MODULE.read_runtime_secret_token(
                Path("/secure/kubeconfig"), "gitlab", "runner-auth", "runner-token"
            )
        except MODULE.BootstrapError as error:
            assert expected in str(error)
        else:
            raise AssertionError("invalid runtime Secret state must fail closed")


def test_image_builder_env_and_vault_keys_are_independent():
    standard = runner_token("abcdefghijklmnopqrstuvwxyz")
    builder = runner_token("zyxwvutsrqponmlkjihgfedcba")
    content = MODULE.replace_env_token("", standard)
    content = MODULE.replace_env_token(
        content, builder, "GITLAB_IMAGE_BUILDER_RUNNER_TOKEN"
    )
    assert MODULE.parse_env_token(content) == standard
    assert (
        MODULE.parse_env_token(
            content, "GITLAB_IMAGE_BUILDER_RUNNER_TOKEN"
        )
        == builder
    )

    yaml_content = MODULE.replace_yaml_token("---\n", standard)
    yaml_content = MODULE.replace_yaml_token(
        yaml_content, builder, "gitlab_image_builder_runner_token"
    )
    assert MODULE.parse_yaml_token(yaml_content) == standard
    assert (
        MODULE.parse_yaml_token(
            yaml_content, "gitlab_image_builder_runner_token"
        )
        == builder
    )

    docker = runner_token("dockerhostabcdefghijklmnop")
    content = MODULE.replace_env_token(
        content, docker, "GITLAB_DOCKER_HOST_RUNNER_TOKEN"
    )
    yaml_content = MODULE.replace_yaml_token(
        yaml_content, docker, "gitlab_docker_host_runner_token"
    )
    assert (
        MODULE.parse_env_token(content, "GITLAB_DOCKER_HOST_RUNNER_TOKEN")
        == docker
    )
    assert (
        MODULE.parse_yaml_token(
            yaml_content, "gitlab_docker_host_runner_token"
        )
        == docker
    )


def test_encrypted_yaml_payload_is_added_and_replaced_without_duplicates():
    token = runner_token("abcdefghijklmnopqrstuvwxyz")
    updated = MODULE.replace_yaml_token("---\nexisting: value\n", token)
    assert updated.endswith(f'gitlab_runner_token: "{token}"\n')
    replacement = runner_token("zyxwvutsrqponmlkjihgfedcba")
    updated = MODULE.replace_yaml_token(updated, replacement)
    assert updated.count("gitlab_runner_token:") == 1
    assert MODULE.parse_yaml_token(updated) == replacement


def test_first_cluster_empty_and_null_yaml_tokens_are_replaced_in_place():
    token = runner_token("abcdefghijklmnopqrstuvwxyz")
    for empty_value in ('""', "''", "null", "Null", "NULL", "~", ""):
        original = f"---\ngitlab_runner_token: {empty_value}\nexisting: value\n"
        updated = MODULE.replace_yaml_token(original, token)
        assert updated.count("gitlab_runner_token:") == 1
        assert MODULE.parse_yaml_token(updated) == token
        assert "existing: value" in updated


def test_valid_quoted_and_unquoted_yaml_token_scalars_are_recognized():
    token = runner_token("abcdefghijklmnopqrstuvwxyz")
    for scalar in (token, f'"{token}"', f"'{token}'"):
        assert MODULE.parse_yaml_token(f"gitlab_runner_token: {scalar}\n") == token

    for scalar in (DOTTED_TOKEN, f'"{DOTTED_TOKEN}"', f"'{DOTTED_TOKEN}'"):
        assert MODULE.parse_yaml_token(f"gitlab_runner_token: {scalar}\n") == DOTTED_TOKEN


def test_duplicate_yaml_token_keys_fail_closed():
    existing = runner_token("abcdefghijklmnopqrstuvwxyz")
    replacement = runner_token("zyxwvutsrqponmlkjihgfedcba")
    content = (
        'gitlab_runner_token: ""\n'
        f'gitlab_runner_token: "{existing}"\n'
    )
    for operation in (MODULE.parse_yaml_token, lambda value: MODULE.replace_yaml_token(
        value, replacement
    )):
        try:
            operation(content)
        except MODULE.BootstrapError as error:
            assert "duplicate" in str(error)
        else:
            raise AssertionError("duplicate GitLab Runner token keys must fail closed")


def test_complex_or_malformed_yaml_token_values_fail_closed():
    malformed = (
        f"[{runner_token('invalid')}]",
        f"{{token: {runner_token('invalid')}}}",
        "|",
        ">-",
        '"unterminated',
        "'unterminated",
        f"&token {runner_token('invalid')}",
        "!vault encrypted",
        "legacy-runner-token",
        runner_token("short"),
        '"legacy-runner-token"',
    )
    for value in malformed:
        content = f"gitlab_runner_token: {value}\n"
        try:
            MODULE.replace_yaml_token(
                content, runner_token("abcdefghijklmnopqrstuvwxyz")
            )
        except MODULE.BootstrapError:
            pass
        else:
            raise AssertionError(f"malformed YAML token value was accepted: {value}")


def test_nested_or_quoted_yaml_token_keys_fail_closed():
    for content in (
        '  gitlab_runner_token: ""\n',
        '"gitlab_runner_token": ""\n',
        "'gitlab_runner_token': null\n",
    ):
        try:
            MODULE.replace_yaml_token(
                content, runner_token("abcdefghijklmnopqrstuvwxyz")
            )
        except MODULE.BootstrapError as error:
            assert "top-level scalar" in str(error)
        else:
            raise AssertionError("non-canonical GitLab Runner token key was accepted")


def test_token_format_rejects_legacy_and_short_values():
    assert MODULE.TOKEN_RE.fullmatch(runner_token("abcdefghijklmnopqrstuvwxyz"))
    assert MODULE.TOKEN_RE.fullmatch(DOTTED_TOKEN)
    assert not MODULE.TOKEN_RE.fullmatch("legacy-abcdefghijklmnopqrstuvwxyz")
    assert not MODULE.TOKEN_RE.fullmatch(runner_token("short"))
    for invalid in (
        runner_token(".abcdefghijklmnop.qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop.qrstuvwxyzABCDEF."),
        runner_token("abcdefghijklmnop..qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop/qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop:qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop;qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop$qrstuvwxyzABCDEF"),
        runner_token("abcdefghijklmnop\\qrstuvwxyzABCDEF"),
    ):
        assert not MODULE.TOKEN_RE.fullmatch(invalid)


def test_secret_values_are_never_placed_in_subprocess_arguments():
    source = SCRIPT.read_text(encoding="utf-8")
    assert 'stdin=source' in source
    assert 'request[\'PRIVATE-TOKEN\'] = ' in source
    assert '"--", "gitlab-rails", "runner", "-"' in source
    assert "--header" not in source
    assert "--form" not in source
    assert "print(runner_token)" not in source
    assert "sensitive command output suppressed" in source


def test_workflow_uses_supported_runner_api_and_revokes_bootstrap_pat():
    source = SCRIPT.read_text(encoding="utf-8")
    assert "/api/v4/user/runners" in source
    assert "scopes: ['create_runner']" in source
    assert "personal_access_tokens.where(name:" in source
    assert "token.revoke! if token && !token.revoked?" in source
    assert "assert_bootstrap_pat_revoked" in source
    assert "/api/v4/runners/verify" in source
    assert "17.1-19.x" in source


def test_single_managed_runner_token_is_recovered_from_captured_rails_output():
    helper = bootstrapper()
    token = DOTTED_TOKEN
    stages = []

    def rails(_pod, _source, *, stage, check=True):
        stages.append((stage, check))
        return subprocess.CompletedProcess([], 0, json.dumps({"count": 1, "token": token}), "")

    helper.rails = rails
    assert helper.recover_managed_runner_token("toolbox") == token
    assert stages == [("managed GitLab Runner recovery", True)]


def test_token_verification_distinguishes_invalid_token_from_transport_failure():
    helper = bootstrapper()
    responses = iter((200, 403, 404, 503))

    def rails(_pod, _source, *, stage, check=True):
        assert stage == "GitLab Runner token verification"
        assert check is True
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"status": next(responses)}), ""
        )

    helper.rails = rails
    assert helper.verify_token("toolbox", DOTTED_TOKEN) is True
    assert helper.verify_token("toolbox", DOTTED_TOKEN) is False
    assert helper.verify_token("toolbox", DOTTED_TOKEN) is False
    try:
        helper.verify_token("toolbox", DOTTED_TOKEN)
    except MODULE.BootstrapError as error:
        assert "unexpected HTTP 503" in str(error)
    else:
        raise AssertionError("server failures must not be mistaken for invalid credentials")


def test_token_verification_transport_error_is_not_downgraded_to_invalid():
    helper = bootstrapper()

    def rails(*_args, **_kwargs):
        raise MODULE.BootstrapError(
            "GitLab Runner token verification failed: sensitive command output suppressed"
        )

    helper.rails = rails
    try:
        helper.verify_token("toolbox", DOTTED_TOKEN)
    except MODULE.BootstrapError as error:
        assert "token verification failed" in str(error)
    else:
        raise AssertionError("transport failure must stop before runner creation")


def test_dotted_provider_token_is_accepted_from_runner_create_response():
    helper = bootstrapper()

    def rails(_pod, _source, *, stage, check=True):
        if stage == "GitLab instance Runner API creation":
            return subprocess.CompletedProcess([], 0, DOTTED_TOKEN, "")
        if stage == "bootstrap PAT revoked-state assertion":
            return subprocess.CompletedProcess(
                [], 0, json.dumps({"count": 1, "id": 7, "revoked": True}), ""
            )
        return subprocess.CompletedProcess([], 0, "", "")

    helper.rails = rails
    assert helper.create_runner_token("toolbox") == DOTTED_TOKEN


def test_image_builder_runner_api_is_protected_tagged_and_not_untagged():
    helper = bootstrapper()
    helper.runner_tags = "image-build"
    helper.run_untagged = False
    helper.access_level = "ref_protected"
    create_source = ""

    def rails(_pod, source, *, stage, check=True):
        nonlocal create_source
        if stage == "GitLab instance Runner API creation":
            create_source = source
            return subprocess.CompletedProcess([], 0, DOTTED_TOKEN, "")
        if stage == "bootstrap PAT revoked-state assertion":
            return subprocess.CompletedProcess(
                [], 0, json.dumps({"count": 1, "id": 7, "revoked": True}), ""
            )
        return subprocess.CompletedProcess([], 0, "", "")

    helper.rails = rails
    assert helper.create_runner_token("toolbox") == DOTTED_TOKEN
    assert "'tag_list' => \"image-build\"" in create_source
    assert "'run_untagged' => \"false\"" in create_source
    assert "'access_level' => \"ref_protected\"" in create_source


def test_no_managed_runner_allows_creation_but_multiple_fail_closed():
    helper = bootstrapper()
    responses = iter(({"count": 0}, {"count": 2}))

    def rails(_pod, _source, *, stage, check=True):
        return subprocess.CompletedProcess([], 0, json.dumps(next(responses)), "")

    helper.rails = rails
    assert helper.recover_managed_runner_token("toolbox") is None
    try:
        helper.recover_managed_runner_token("toolbox")
    except MODULE.BootstrapError as error:
        assert "found 2 runners" in str(error)
        assert "refusing to choose" in str(error)
    else:
        raise AssertionError("multiple managed runners must fail closed")


def test_managed_runner_requires_a_recoverable_glrt_token():
    helper = bootstrapper()
    helper.rails = lambda *_args, **_kwargs: subprocess.CompletedProcess(
        [], 0, json.dumps({"count": 1, "token": "legacy-token"}), ""
    )
    try:
        helper.recover_managed_runner_token("toolbox")
    except MODULE.BootstrapError as error:
        assert "no recoverable glrt token" in str(error)
    else:
        raise AssertionError("an invalid managed runner token must fail closed")


def test_nonzero_pat_revoke_is_accepted_only_after_exact_revoked_proof():
    helper = bootstrapper()
    stages = []

    def rails(_pod, _source, *, stage, check=True):
        stages.append((stage, check))
        if stage == "bootstrap PAT revocation":
            return subprocess.CompletedProcess([], 9, "", "transport closed")
        return subprocess.CompletedProcess([], 0, json.dumps({"count": 1, "id": 7, "revoked": True}), "")

    helper.rails = rails
    helper.revoke_bootstrap_pat("toolbox", "unique-pat")
    assert stages == [
        ("bootstrap PAT revocation", False),
        ("bootstrap PAT revoked-state assertion", True),
    ]


def test_nonzero_pat_revoke_fails_when_exact_revoked_state_is_not_proven():
    helper = bootstrapper()

    def rails(_pod, _source, *, stage, check=True):
        if stage == "bootstrap PAT revocation":
            return subprocess.CompletedProcess([], 9, "", "transport closed")
        return subprocess.CompletedProcess([], 0, json.dumps({"count": 1, "id": 7, "revoked": False}), "")

    helper.rails = rails
    try:
        helper.revoke_bootstrap_pat("toolbox", "unique-pat")
    except MODULE.BootstrapError as error:
        assert "exited nonzero" in str(error)
        assert "could not be proven" in str(error)
    else:
        raise AssertionError("unproven PAT revocation must fail closed")


def test_successful_pat_revoke_still_requires_exact_revoked_state():
    helper = bootstrapper()

    def rails(_pod, _source, *, stage, check=True):
        if stage == "bootstrap PAT revocation":
            return subprocess.CompletedProcess([], 0, "", "")
        return subprocess.CompletedProcess([], 0, json.dumps({"count": 0}), "")

    helper.rails = rails
    try:
        helper.revoke_bootstrap_pat("toolbox", "unique-pat")
    except MODULE.BootstrapError as error:
        assert "expected exactly one" in str(error)
    else:
        raise AssertionError("a missing PAT record cannot prove exact revocation")


def test_sensitive_failures_report_stage_without_subprocess_output(monkeypatch):
    def failed_run(*_args, **_kwargs):
        return subprocess.CompletedProcess([], 1, "secret-stdout", "secret-stderr")

    monkeypatch.setattr(MODULE.subprocess, "run", failed_run)
    try:
        MODULE.run_command(
            ["kubectl", "exec"],
            stdin="secret-stdin",
            sensitive=True,
            label="managed GitLab Runner recovery",
        )
    except MODULE.BootstrapError as error:
        message = str(error)
        assert message.startswith("managed GitLab Runner recovery failed:")
        assert "secret-stdout" not in message
        assert "secret-stderr" not in message
    else:
        raise AssertionError("sensitive subprocess failure must be labeled")


def test_cluster_lease_is_atomically_created_renewable_and_safely_released(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"], renew_interval_seconds=3600)
    current = lease_document(lock.holder)
    replacements = []

    def run(argv, *, stdin=None, **_kwargs):
        if "create" in argv:
            manifest = json.loads(stdin)
            assert manifest["spec"]["holderIdentity"] == lock.holder
            assert runner_token("") not in stdin and "glpat-" not in stdin
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        if "get" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        if "replace" in argv:
            replacements.append(json.loads(stdin))
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(argv)

    monkeypatch.setattr(MODULE, "run_command", run)
    lock.acquire()
    lock._renew_once()
    lock.release()
    assert replacements[0]["metadata"]["resourceVersion"] == "1"
    assert replacements[0]["spec"]["holderIdentity"] == lock.holder
    released = replacements[-1]
    assert released["metadata"]["resourceVersion"] == "1"
    assert "holderIdentity" not in released["spec"]
    assert released["spec"]["leaseDurationSeconds"] == 1


def test_active_cluster_lease_rejects_a_concurrent_bootstrap(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"])
    active = lease_document("runner-bootstrap-other")

    def run(argv, **_kwargs):
        if "create" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "AlreadyExists")
        return subprocess.CompletedProcess(argv, 0, json.dumps(active), "")

    monkeypatch.setattr(MODULE, "run_command", run)
    try:
        lock.acquire()
    except MODULE.BootstrapError as error:
        assert "another GitLab Runner bootstrap currently holds" in str(error)
    else:
        raise AssertionError("an active cluster-wide Lease must serialize bootstrap")


def test_absent_lease_after_failed_create_retries_without_parsing_notfound_text(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(
        ["kubectl", "-n", "gitlab"], renew_interval_seconds=3600
    )
    current = lease_document(lock.holder)
    create_attempts = 0
    observed_real_failure = subprocess.CompletedProcess(
        ["kubectl", "get", "lease"],
        1,
        "",
        'Error from server (NotFound): leases.coordination.k8s.io '
        '"ansible-k8s-runner-bootstrap" not found',
    )

    def run(argv, **_kwargs):
        nonlocal create_attempts
        if "create" in argv:
            create_attempts += 1
            if create_attempts == 1:
                return subprocess.CompletedProcess(argv, 1, "", "transient create failure")
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        if "get" in argv and "--ignore-not-found" in argv:
            # The exact live failure above represented this same absent object.
            # Structured ignore-not-found converts it to rc=0 plus empty stdout,
            # so no localized stderr string enters the acquisition decision.
            assert observed_real_failure.returncode == 1
            assert "leases.coordination.k8s.io" in observed_real_failure.stderr
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "get" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        if "replace" in argv:
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(argv)

    monkeypatch.setattr(MODULE, "run_command", run)
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    lock.acquire()
    assert create_attempts == 2
    lock.release()


def test_persistently_absent_lease_reports_original_create_failure(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"])

    def run(argv, **_kwargs):
        if "create" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "forbidden to create leases")
        if "get" in argv:
            assert "--ignore-not-found" in argv
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)

    monkeypatch.setattr(MODULE, "run_command", run)
    monkeypatch.setattr(MODULE.time, "sleep", lambda _seconds: None)
    try:
        lock.acquire()
    except MODULE.BootstrapError as error:
        assert "atomically create the absent bootstrap Lease" in str(error)
        assert "forbidden to create leases" in str(error)
    else:
        raise AssertionError("persistent absent-Lease creation failure must fail closed")


def test_expired_cluster_lease_is_taken_over_with_resource_version(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"], renew_interval_seconds=3600)
    current = lease_document("runner-bootstrap-dead", resource_version="9", expired=True)
    takeover = []

    def run(argv, *, stdin=None, **_kwargs):
        nonlocal current
        if "create" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "AlreadyExists")
        if "get" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        if "replace" in argv:
            manifest = json.loads(stdin)
            takeover.append(json.loads(json.dumps(manifest)))
            current = manifest
            current["metadata"]["resourceVersion"] = "10"
            return subprocess.CompletedProcess(argv, 0, json.dumps(current), "")
        raise AssertionError(argv)

    monkeypatch.setattr(MODULE, "run_command", run)
    lock.acquire()
    assert takeover[0]["metadata"]["resourceVersion"] == "9"
    assert takeover[0]["spec"]["holderIdentity"] == lock.holder
    lock.release()


def test_release_never_modifies_a_successor_lease(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"])
    successor = lease_document("runner-bootstrap-successor", resource_version="22")
    actions = []

    def run(argv, **_kwargs):
        actions.append(argv)
        return subprocess.CompletedProcess(argv, 0, json.dumps(successor), "")

    monkeypatch.setattr(MODULE, "run_command", run)
    lock.release()
    assert len(actions) == 1
    assert "get" in actions[0]
    assert all("replace" not in argv for argv in actions)


def test_lost_or_malformed_cluster_lease_fails_closed(monkeypatch):
    lock = MODULE.KubernetesLeaseLock(["kubectl", "-n", "gitlab"])
    documents = iter(
        (
            lease_document("runner-bootstrap-successor"),
            {
                "metadata": {
                    "name": "ansible-k8s-runner-bootstrap",
                    "resourceVersion": "2",
                },
                "spec": {"holderIdentity": lock.holder, "leaseDurationSeconds": 900},
            },
        )
    )

    def run(argv, **_kwargs):
        return subprocess.CompletedProcess(argv, 0, json.dumps(next(documents)), "")

    monkeypatch.setattr(MODULE, "run_command", run)
    for expected in ("ownership was lost", "renewal timestamp"):
        try:
            lock.assert_held()
        except MODULE.BootstrapError as error:
            assert expected in str(error)
        else:
            raise AssertionError("invalid Lease state must fail closed")


def test_main_protects_recovery_creation_and_both_persistence_targets_with_lease():
    source = SCRIPT.read_text(encoding="utf-8")
    protected = source.split(
        "KubernetesLeaseLock(bootstrapper.kubectl) as bootstrap_lock", 1
    )[1].split('print(f"GitLab {version}:', 1)[0]
    assert "LocalSecretsFileLock(args.secrets_file)" in protected
    for operation in (
        "recover_managed_runner_token",
        "read_runtime_secret_token",
        "create_runner_token",
        "encrypt_secrets",
        "atomic_write",
    ):
        assert operation in protected
    assert protected.count("bootstrap_lock.assert_held()") >= 3
