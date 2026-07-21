# Persistent test DR endpoint

`scripts/test-dr-endpoint.sh` creates a campaign-scoped MinIO endpoint for live
backup and recovery tests. Compute is disposable; backup data is not. MinIO
uses a dedicated Hetzner volume named `<campaign>-dr-data`, labeled with its
campaign and purpose, and protected against deletion at the provider.

## Lifecycle

```bash
# Create a server or recreate it around the retained volume.
eval "$(./scripts/test-dr-endpoint.sh up lab01 | grep '^export ')"

# Remove the server, firewall, temporary SSH key, and DNS record. Keep data.
./scripts/test-dr-endpoint.sh down lab01

# Recreate the endpoint and reattach the same MinIO data without formatting it.
eval "$(./scripts/test-dr-endpoint.sh up lab01 | grep '^export ')"
```

On first creation, the helper marks the volume initialization as `pending` at
the provider and formats only that owned, signature-free volume as ext4. This
also makes a failure before formatting safely resumable. Once MinIO is ready,
the provider label advances to `ready`. Later runs require the existing
filesystem and campaign marker, mount it by filesystem UUID, and refuse to
adopt or format ambiguous storage. A failed `up` removes partial compute but
retains the delete-protected volume.

The provider may reassign a recently released public IP to the replacement
server. After creating the new server through the authenticated API, the helper
removes only that exact IP's obsolete entry from its campaign-scoped
`known_hosts` file, then accepts and records the replacement key. It never
relaxes host-key checking globally.

`down` is therefore safe for ordinary cleanup and server-recreation drills. It
does **not** erase backup objects. Confirm that independent recovery evidence
is no longer needed before using the destructive operation:

```bash
./scripts/test-dr-endpoint.sh purge lab01 "PURGE lab01 DR DATA"
```

The phrase is exact and campaign-specific. `purge` first removes compute, then
validates the volume ownership labels, disables provider delete protection,
deletes the volume, and verifies absence. A mismatched or missing phrase causes
no mutation.

## Configuration and security

- `TEST_DR_VOLUME_SIZE_GB` sets volume capacity and defaults to the Hetzner
  minimum of 10 GiB.
- `TEST_DR_LOCATION`, `TEST_DR_SERVER_TYPE`, `TEST_DR_DNS_ZONE`, and
  `TEST_DR_BUCKET` retain their existing meanings.
- Credentials are loaded from the repository-local, gitignored `.env`. The
  helper never prints them and the local state file contains only non-secret
  endpoint, bucket, project, domain, and volume identifiers.
- Rotate test credentials after a campaign and after any diagnostic session in
  which process or container configuration may have been exposed.
