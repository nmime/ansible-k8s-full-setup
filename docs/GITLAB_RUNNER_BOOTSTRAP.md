# GitLab Runner authentication bootstrap

The platform uses GitLab's runner authentication tokens (`glrt-...`), not the
legacy registration-token workflow removed from current GitLab releases. An
enabled `gitlab.runner.enabled` selector fails closed until a valid token is
available in the process environment or encrypted platform secrets.

## Existing GitLab instance

Prerequisites:

- the GitLab Toolbox Pod is Ready;
- the controller kubeconfig and Ansible Vault password file have mode `0600`
  or stricter;
- `.platform-secrets.yml` already exists and is Ansible Vault encrypted;
- `.env` is inside this repository, Git-ignored, and mode `0600` if it exists.

Run:

```bash
scripts/bootstrap-gitlab-runner-token.py \
  --kubeconfig /absolute/path/to/kubeconfig \
  --secrets-file /absolute/path/to/.platform-secrets.yml \
  --vault-password-file /absolute/path/to/vault-password
```

For an isolated campaign, pass its operator-state secrets path. The default is
`playbooks/.platform-secrets.yml`. The helper prints only the GitLab version
and whether it reused or created a credential; it never prints token values.

The idempotent workflow is:

1. Decrypt the platform secrets into controller memory and compare the token
   with the ignored `.env` value.
2. Verify each candidate through `POST /api/v4/runners/verify`, including the
   system ID required for `glrt-` tokens. A valid single candidate is reused.
3. If no candidate is live, run the documented Rails token operation inside
   the Toolbox Pod to create a one-day root personal access token restricted to
   the `create_runner` scope.
4. Call GitLab's supported `POST /api/v4/user/runners` endpoint from inside the
   Toolbox Pod to create an auditable instance runner and receive its one-time
   authentication token.
5. Revoke the short-lived personal access token in an unconditional cleanup
   step, then atomically update the Ansible Vault-encrypted secrets file and
   Git-ignored `.env`.

Rails source and all secret values travel over `kubectl exec -i` standard
input. API responses and Vault plaintext are captured pipes. No PAT or runner
token appears in a process argument, Ansible output, command log, or terminal
output. The only temporary plaintext file is mode `0600` in a mode `0700`
directory used as input to `ansible-vault encrypt`; it is removed before the
helper exits. A failure never reports sensitive subprocess output.

If `.env` and encrypted secrets contain two different tokens that both verify,
the helper stops instead of choosing an identity silently. If persistence is
interrupted after GitLab creates a runner, rerun the helper; stale unused runner
records can then be removed by an administrator after the active runner is
confirmed.

## First cluster bootstrap

A new cluster cannot create an authentication token before GitLab exists. Use
an explicit two-phase bootstrap:

```bash
cd platform-orchestrator
./platform.sh disable gitlab-runner
./platform.sh deploy all

../scripts/bootstrap-gitlab-runner-token.py \
  --kubeconfig "$KUBECONFIG" \
  --secrets-file ../playbooks/.platform-secrets.yml \
  --vault-password-file "$ANSIBLE_VAULT_PASSWORD_FILE"

./platform.sh enable gitlab-runner
./platform.sh deploy gitlab-runner
```

Disabling the selector does not remove GitLab. Enabling it after the token gate
reconciles the separate Runner Helm release.

## Version compatibility

The helper requires GitLab 17.1 through 19.x and fails closed outside that
range. GitLab 17.1 introduced the restricted `create_runner` token scope used
for the bootstrap PAT. The repository-pinned GitLab chart `10.1.2` has
`appVersion: v19.1.2` and is inside the validated range. The workflow uses the
public runner APIs introduced in GitLab 16.0 and the documented self-managed
Rails operations for creating and revoking a personal access token.

Primary references:

- [Create a runner linked to a user](https://docs.gitlab.com/api/users/#create-a-runner-linked-to-a-user)
- [Runner authentication and verification API](https://docs.gitlab.com/api/runners/)
- [New runner creation workflow](https://docs.gitlab.com/ci/runners/new_creation_workflow/)
- [Programmatic personal access tokens](https://docs.gitlab.com/user/profile/personal_access_tokens/#create-a-personal-access-token-programmatically)
- [Access-token scopes](https://docs.gitlab.com/security/tokens/access_token_scopes/)
