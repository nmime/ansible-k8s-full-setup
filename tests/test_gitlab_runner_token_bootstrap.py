"""Contracts for the GitLab Runner token bootstrap helper."""

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/bootstrap-gitlab-runner-token.py"
SPEC = spec_from_file_location("gitlab_runner_bootstrap", SCRIPT)
assert SPEC and SPEC.loader
MODULE = module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_env_assignment_is_added_and_replaced_without_duplicates():
    token = "glrt-abcdefghijklmnopqrstuvwxyz"
    assert MODULE.replace_env_token("HCLOUD_TOKEN=x\n", token).endswith(
        f"GITLAB_RUNNER_TOKEN='{token}'\n"
    )
    updated = MODULE.replace_env_token(
        "GITLAB_RUNNER_TOKEN='glrt-oldoldoldoldoldold'\nKEEP=yes\n", token
    )
    assert updated.count("GITLAB_RUNNER_TOKEN=") == 1
    assert MODULE.parse_env_token(updated) == token


def test_encrypted_yaml_payload_is_added_and_replaced_without_duplicates():
    token = "glrt-abcdefghijklmnopqrstuvwxyz"
    updated = MODULE.replace_yaml_token("---\nexisting: value\n", token)
    assert updated.endswith(f'gitlab_runner_token: "{token}"\n')
    updated = MODULE.replace_yaml_token(updated, "glrt-zyxwvutsrqponmlkjihgfedcba")
    assert updated.count("gitlab_runner_token:") == 1
    assert MODULE.parse_yaml_token(updated) == "glrt-zyxwvutsrqponmlkjihgfedcba"


def test_first_cluster_empty_and_null_yaml_tokens_are_replaced_in_place():
    token = "glrt-abcdefghijklmnopqrstuvwxyz"
    for empty_value in ('""', "''", "null", "Null", "NULL", "~", ""):
        original = f"---\ngitlab_runner_token: {empty_value}\nexisting: value\n"
        updated = MODULE.replace_yaml_token(original, token)
        assert updated.count("gitlab_runner_token:") == 1
        assert MODULE.parse_yaml_token(updated) == token
        assert "existing: value" in updated


def test_valid_quoted_and_unquoted_yaml_token_scalars_are_recognized():
    token = "glrt-abcdefghijklmnopqrstuvwxyz"
    for scalar in (token, f'"{token}"', f"'{token}'"):
        assert MODULE.parse_yaml_token(f"gitlab_runner_token: {scalar}\n") == token


def test_duplicate_yaml_token_keys_fail_closed():
    content = (
        'gitlab_runner_token: ""\n'
        'gitlab_runner_token: "glrt-abcdefghijklmnopqrstuvwxyz"\n'
    )
    for operation in (MODULE.parse_yaml_token, lambda value: MODULE.replace_yaml_token(
        value, "glrt-zyxwvutsrqponmlkjihgfedcba"
    )):
        try:
            operation(content)
        except MODULE.BootstrapError as error:
            assert "duplicate" in str(error)
        else:
            raise AssertionError("duplicate GitLab Runner token keys must fail closed")


def test_complex_or_malformed_yaml_token_values_fail_closed():
    malformed = (
        "[glrt-invalid]",
        "{token: glrt-invalid}",
        "|",
        ">-",
        '"unterminated',
        "'unterminated",
        "&token glrt-invalid",
        "!vault encrypted",
        "legacy-runner-token",
        "glrt-short",
        '"legacy-runner-token"',
    )
    for value in malformed:
        content = f"gitlab_runner_token: {value}\n"
        try:
            MODULE.replace_yaml_token(content, "glrt-abcdefghijklmnopqrstuvwxyz")
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
            MODULE.replace_yaml_token(content, "glrt-abcdefghijklmnopqrstuvwxyz")
        except MODULE.BootstrapError as error:
            assert "top-level scalar" in str(error)
        else:
            raise AssertionError("non-canonical GitLab Runner token key was accepted")


def test_token_format_rejects_legacy_and_short_values():
    assert MODULE.TOKEN_RE.fullmatch("glrt-abcdefghijklmnopqrstuvwxyz")
    assert not MODULE.TOKEN_RE.fullmatch("legacy-abcdefghijklmnopqrstuvwxyz")
    assert not MODULE.TOKEN_RE.fullmatch("glrt-short")


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
    assert "PersonalAccessToken.find_by_token" in source
    assert "token.revoke! if token" in source
    assert "/api/v4/runners/verify" in source
    assert "17.1-19.x" in source
