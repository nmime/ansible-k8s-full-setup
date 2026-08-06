# object-storage

Deploys the active SeaweedFS S3-compatible object storage implementation.

## Key variables

- `object_storage_enabled` — default `true`
- `object_storage_namespace` — default `storage`
- `object_storage_release_name` — default `seaweedfs`

## Where applied

Included by `playbooks/deploy_platform.yml` for `small` and larger profiles.
Consumed by GitLab, backup-restore, and other components needing S3.
