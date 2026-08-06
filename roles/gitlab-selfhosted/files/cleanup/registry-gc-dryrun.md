# GitLab Container Registry GC — Dry-run procedure

**Never run the destructive pass without a reviewed dry-run first.**

1. Shell into the registry pod:
   `kubectl -n gitlab exec -it deploy/gitlab-registry -- bash`
2. **Dry run** (marks what *would* be deleted, no writes):
   ```bash
   /opt/gitlab/embedded/bin/registry garbage-collect -m --dry-run /etc/gitlab/registry/config.yml
   ```
   (`-m` = remove untagged manifests). Inspect output; confirm only stale/unreferenced blobs listed.
3. If clean, schedule the real run during a maintenance window (jobs that push
   images will fail mid-GC — pause CI pushes first):
   ```bash
   /opt/gitlab/embedded/bin/registry garbage-collect -m /etc/gitlab/registry/config.yml
   ```
4. Verify reclaimed space: `kubectl -n gitlab exec deploy/gitlab-registry -- du -sh /var/opt/gitlab/registry`
5. Optional: automate the *dry-run* only as a weekly CronJob; keep the real pass manual.

This is a **doc only** — not applied to the cluster.
