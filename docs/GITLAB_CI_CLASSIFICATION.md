# GitLab CI classification

The shared Runner pool is selected with `ci-shared`. Jobs are classified from
GitLab ref metadata, not from a hard-coded branch such as `preproduction`:

| GitLab context | CI class | Kubernetes PriorityClass |
|---|---|---|
| protected schedule | maintenance | `n0xeid-ci-maintenance` |
| default protected branch or protected tag | production | `n0xeid-ci-production` |
| any other protected branch | environment | `n0xeid-ci-environment` |
| merge request or unprotected branch | review | `n0xeid-ci-review` |

All CI PriorityClasses are negative and use `preemptionPolicy: Never`.
Platform and application classes are positive, so services reclaim worker
capacity ahead of CI without binding jobs to named nodes.

Include `templates/gitlab-ci-environment-classification.yml` and extend
`.n0xeid-ci-auto` for test/build jobs. Deployment jobs extend
`.n0xeid-deploy-production` or `.n0xeid-deploy-environment` and must declare a
GitLab environment using `deployment_tier`. The production guard fails closed
unless both the ref and canonical environment tier are production-safe.

The general pool deliberately does not accept `image-build` or `docker-host`.
Those tags retain isolated runners because rootless BuildKit still requires a
narrow unconfined build-container exception and Docker-in-Docker is privileged.
Move a pipeline off those tags only after its build no longer requires those
kernel privileges.
