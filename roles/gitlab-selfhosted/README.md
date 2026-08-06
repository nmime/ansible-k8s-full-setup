# gitlab-selfhosted

Deploys GitLab CE with Gitaly, Container Registry, KAS, Toolbox, and an
optional GitLab Runner. Uses PostgreSQL, Dragonfly, and SeaweedFS object
storage.

## Key variables

Tier-specific variables are set dynamically based on the active profile.
Key inputs include chart version, image pins, resource requests/limits, and
storage class.

## Where applied

Included by `playbooks/deploy_platform.yml` for `small` and larger profiles.
