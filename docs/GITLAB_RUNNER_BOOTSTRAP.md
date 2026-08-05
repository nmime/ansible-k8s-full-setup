# GitLab Runner authentication bootstrap

The platform uses GitLab's runner authentication tokens (`glrt-...`), including
the dot-segmented tokens issued by GitLab 19, not the
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

1. Atomically acquire the `ansible-k8s-runner-bootstrap` Kubernetes Lease in
   the GitLab namespace. Its holder is a random, non-secret UUID, it is renewed
   every 60 seconds, and it expires after 15 minutes without renewal.
2. Decrypt the platform secrets into controller memory and compare the token
   with the ignored `.env` value.
3. Verify each candidate through `POST /api/v4/runners/verify`, including the
   system ID required for `glrt-` tokens. A valid single candidate is reused.
4. If no candidate is live, run the documented Rails token operation inside
   the Toolbox Pod to create a one-day root personal access token restricted to
   the `create_runner` scope.
5. Call GitLab's supported `POST /api/v4/user/runners` endpoint from inside the
   Toolbox Pod to create an auditable instance runner and receive its one-time
   authentication token.
6. Revoke the short-lived personal access token in an unconditional cleanup
   step, then atomically update the Ansible Vault-encrypted secrets file and
   Git-ignored `.env`.

The Lease covers recovery, creation, verification, and both persistence
targets. A concurrent helper fails before inspecting or creating runners. A
crashed holder is taken over only after expiry and with Kubernetes
`resourceVersion` optimistic concurrency. Normal cleanup releases by replacing
the exact owned Lease with an immediately available record; it never deletes a
Lease and therefore cannot remove a successor's lock. No credential is stored
in the Lease or appears in its holder identity.

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

## Protected Docker compatibility worker

Jobs tagged `docker-host` use a separate protected runner identity. Set
`gitlab.runner.docker_host.dedicated_worker_index` to a worker included in
`infrastructure.workers.count`. The playbook:

1. creates that server without public IP addresses and labels its provider role
   `ci-docker-worker`;
2. adds a Kubernetes label plus a `NoSchedule` taint;
3. excludes it from public load-balancer targets and local-PV discovery;
4. pins the only allowed build image and DinD service by digest;
5. strategically patches only the service container named `docker` as
   privileged; and
6. refuses runner reconciliation unless exactly one correctly tainted node
   matches the configured worker index.

The advanced PodSpec patch fails closed: changing the CI service alias means
the expected `docker` container is not patched and the job cannot start DinD.
Do not enable Kubernetes-executor-wide `privileged`; that would also privilege
the build and helper containers. Keep `concurrent_jobs: 1` for this runner.

## General and rootless image-build worker

Set `gitlab.runner.dedicated_worker_index` to a different worker for ordinary
Kubernetes-executor jobs and the protected rootless BuildKit runner. The
playbook labels and taints that node with
`workload.n0xeid.xyz/ci-build=true:NoSchedule`, excludes it from ingress and
the local-PV pool, and fails runner reconciliation unless exactly one matching
node exists. Both runner classes receive the matching selector and toleration;
application workloads receive neither.

For the medium-optimized profile, use CPX42 or larger and keep the general
runner at one replica/concurrent job. Its 320 GB SSD prevents container-image
snapshots from consuming the 160 GB application-worker disks. The protected
image runner remains a separate identity and namespace with concurrency one.

When a smaller worker is selected, set
`gitlab.runner.job_resources.memory_request` and
`gitlab.runner.image_builder.job_resources.memory_request` so the sum of the
two build Pods (including helper requests) is greater than node allocatable
memory. Kubernetes then serializes the independently tagged queues instead of
allowing simultaneous jobs to exhaust the node. Keep each request below the
single-node allocatable value and confirm it against `kubectl describe node`
before changing the dedicated worker.

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
