#!/usr/bin/env python3
"""Idempotent GitLab group, project, ACL, and bot provisioning.

This is the single source of truth for the n0xeid GitLab instance access model.
It ensures the group tree, project placement, role-based access control, branch
protection, default-branch settings, and bot accounts are provisioned to spec.

Environment
-----------
    GITLAB_TOKEN        Root (or admin) personal access token. Required.
    GITLAB_URL          GitLab base URL. Default: https://git.n0xeid.xyz
    GITLAB_TOKENS_DIR   Directory to write bot PAT files (mode 0600).
                        Default: /Users/nmi/.splox-secrets
    PAT_EXPIRY_DAYS     Bot PAT lifetime in days. Default: 90

Usage
-----
    # Dry run: report what would change, change nothing.
    GITLAB_TOKEN=<root-pat> python3 scripts/gitlab-acls.py

    # Apply changes live.
    GITLAB_TOKEN=<root-pat> python3 scripts/gitlab-acls.py --apply

    # Verify only: read current state, compare to spec, exit non-zero on drift.
    GITLAB_TOKEN=<root-pat> python3 scripts/gitlab-acls.py --verify

Design notes
------------
* Idempotent: every reconciliation is check-then-create-or-update. Re-running
  is safe and produces no changes once converged.
* GitLab CE 19.1.2 does not support MR approval rules (Premium feature). The
  gitops "merge only via MR" requirement is enforced through branch protection:
  push_access_level=0 (No one) on main, so direct pushes are rejected and all
  changes must go through a merge request. The 1-approval requirement is
  documented in docs/GITLAB_ACL_MANIFEST.md as a Premium upgrade item.
* Secret handling: bot PAT values are written only to files under
  GITLAB_TOKENS_DIR (mode 0600) and are never printed to stdout/stderr or
  committed. The script stores the token *id* (for later rotation) but not the
  value.
* Legacy repo fun/fun-games (id 1) is NOT deleted or moved. It is documented in
  the manifest and known-debt. See docs/KNOWN-DEBT.md for rationale.

Dependencies: requests (standard library otherwise).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Any

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write(
        "ERROR: 'requests' is required. Install it in the repo venv:\n"
        "  .venv/bin/pip install requests\n"
    )
    raise

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DEFAULT_GITLAB_URL = "https://git.n0xeid.xyz"
DEFAULT_TOKENS_DIR = "/Users/nmi/.splox-secrets"
DEFAULT_PAT_EXPIRY_DAYS = 90

# Access levels (GitLab constants)
GUEST = 10
REPORTER = 20
DEVELOPER = 30
MAINTAINER = 40
OWNER = 50
NO_ACCESS = 0

LEVEL_NAMES = {
    0: "No one",
    10: "Guest",
    20: "Reporter",
    30: "Developer",
    40: "Maintainer",
    50: "Owner",
}

# ---------------------------------------------------------------------------
# Desired state (the spec)
# ---------------------------------------------------------------------------

# Group tree: full_path -> {description, parent_full_path or None}
# Extra subgroups that already exist (acl, team, tools, devops, platform under
# fun/agents/dadya) are left untouched — only the required groups below are
# reconciled.
GROUP_TREE: dict[str, dict[str, str | None]] = {
    # Top-level divisions
    "platform": {
        "description": (
            "Shared platform infrastructure, base Ansible roles, and "
            "cluster-wide services (postal, vault, headscale)"
        ),
        "parent": None,
    },
    "fun": {
        "description": "Fun Games division: application source, GitOps, CI, and access control",
        "parent": None,
    },
    "agents": {
        "description": "Agents division: AI agent applications, platform services, and GitOps",
        "parent": None,
    },
    "dadya": {
        "description": "Dadya division: mining applications and infrastructure",
        "parent": None,
    },
    # fun subgroups (required by spec)
    "fun/development": {
        "description": "Fun Games application source repositories",
        "parent": "fun",
    },
    "fun/argocd": {
        "description": "GitOps source repositories consumed by Argo CD",
        "parent": "fun",
    },
    "fun/apps": {
        "description": "Fun Games application deployment manifests",
        "parent": "fun",
    },
    # agents subgroups (required by spec)
    "agents/apps": {
        "description": "AI agent application source repositories",
        "parent": "agents",
    },
    "agents/argocd": {
        "description": "Agents GitOps source repositories consumed by Argo CD",
        "parent": "agents",
    },
    "agents/platform": {
        "description": "Agents platform infrastructure and shared services",
        "parent": "agents",
    },
    "agents/gitops": {
        "description": "Agents GitOps configuration",
        "parent": "agents",
    },
    # dadya subgroups (required by spec)
    "dadya/apps": {
        "description": "Dadya application source repositories",
        "parent": "dadya",
    },
    "dadya/gitops": {
        "description": "Dadya GitOps configuration",
        "parent": "dadya",
    },
}

# Project registry: path_with_namespace -> metadata
# "class" drives the role matrix and branch-protection policy.
PROJECTS: dict[str, dict[str, Any]] = {
    # --- apps (application source) ---
    "agents/apps/social-agents": {"class": "apps"},
    "agents/apps/steel-browser": {"class": "apps"},
    "dadya/apps/dadya-miner": {"class": "apps"},
    "fun/development/fun-games": {"class": "apps"},
    # --- gitops (GitOps / Argo CD source) ---
    "agents/argocd/ansible-k8s-full-setup-n0xeid": {"class": "gitops"},
    "agents/gitops/cluster": {"class": "gitops"},
    "dadya/gitops/cluster": {"class": "gitops"},
    "fun/argocd/ansbile-k8s-full-setup-n0xeid": {
        "class": "gitops",
        "note": "Path typo 'ansbile' — do NOT rename; see docs/KNOWN-DEBT.md",
    },
    # --- infra (platform infrastructure) ---
    "agents/platform/steel-browser": {"class": "infra"},
    "platform/ansible-k8s-full-setup": {"class": "infra"},
    # --- legacy (do not delete) ---
    "fun/fun-games": {
        "class": "legacy",
        "note": "Legacy repo (id 1) outside convention. DO NOT delete. See docs/KNOWN-DEBT.md.",
    },
}

# Role matrix: per project class, the access level for each principal.
# None means the principal is NOT a member of that class's groups.
ROLE_MATRIX: dict[str, dict[str, int | None]] = {
    #               developers  ops  deploy-bot  app-ci-bot
    "apps": {"developers": DEVELOPER, "ops": MAINTAINER, "deploy-bot": None, "app-ci-bot": DEVELOPER},
    "gitops": {"developers": REPORTER, "ops": MAINTAINER, "deploy-bot": MAINTAINER, "app-ci-bot": None},
    "infra": {"developers": REPORTER, "ops": MAINTAINER, "deploy-bot": None, "app-ci-bot": None},
    "legacy": {"developers": DEVELOPER, "ops": MAINTAINER, "deploy-bot": None, "app-ci-bot": None},
}

# Branch protection policy per project class.
# GitOps repos: push=0 (No one) forces all changes through MRs.
BRANCH_PROTECTION: dict[str, dict[str, int]] = {
    "apps": {"push": MAINTAINER, "merge": MAINTAINER},
    "gitops": {"push": NO_ACCESS, "merge": MAINTAINER},
    "infra": {"push": MAINTAINER, "merge": MAINTAINER},
    "legacy": {"push": MAINTAINER, "merge": MAINTAINER},
}

# Which group each project class maps to for group-level membership of bots.
# Bots are added at the group level so access inherits to all child projects.
GROUPS_BY_CLASS: dict[str, list[str]] = {
    "apps": ["agents/apps", "dadya/apps", "fun/development"],
    "gitops": ["agents/argocd", "agents/gitops", "fun/argocd", "dadya/gitops"],
    "infra": ["agents/platform", "platform"],
    # legacy fun/fun-games is directly under fun — handled per-project
}

# Bot account definitions.
BOTS: dict[str, dict[str, Any]] = {
    "deploy-bot": {
        "name": "Deploy Bot",
        "email": "deploy-bot@n0xeid.xyz",
        "pat_name": "deploy-bot-acl",
        "pat_scopes": ["api", "write_repository"],
        # Applied at group level via GROUPS_BY_CLASS
        "applies_to_classes": ["gitops"],
        "token_file": "gl-deploy-bot-token.txt",
    },
    "app-ci-bot": {
        "name": "App CI Bot",
        "email": "app-ci-bot@n0xeid.xyz",
        "pat_name": "app-ci-bot-acl",
        "pat_scopes": ["api", "write_repository"],
        "applies_to_classes": ["apps"],
        "token_file": "gl-app-ci-bot-token.txt",
    },
}

# Human operators. nmi is the sole operator; kept at Maintainer on all
# relevant groups. Future operators would be added here.
OPS_USERS = ["nmi"]

# Human developers (application/feature contributors). Empty today: no
# developer accounts exist on the instance yet. When a developer account is
# created, add the username here and it will automatically receive:
#   - apps  -> Developer
#   - gitops -> Reporter (read-only)
#   - infra -> Reporter (read-only)
DEVELOPER_USERS: list[str] = []


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------

class GitLabAPI:
    """Thin retrying wrapper over the GitLab REST API."""

    def __init__(self, base_url: str, token: str):
        self.base = base_url.rstrip("/") + "/api/v4"
        self.session = requests.Session()
        self.session.headers.update({"PRIVATE-TOKEN": token})
        self.token = token

    def _request(self, method: str, path: str, **kwargs) -> requests.Response:
        url = f"{self.base}{path}"
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                r = self.session.request(method, url, timeout=120, **kwargs)
                return r
            except requests.RequestException as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(3 + attempt * 2)
        raise last_exc  # type: ignore[misc]

    def get(self, path: str, **params) -> Any:
        """GET that auto-paginates (returns list) unless raw=True."""
        raw = params.pop("raw", False)
        if raw:
            r = self._request("GET", path, params=params)
            r.raise_for_status()
            return r.json()
        out: list = []
        page = 1
        while True:
            p = dict(params, per_page=100, page=page)
            r = self._request("GET", path, params=p)
            r.raise_for_status()
            batch = r.json()
            if isinstance(batch, dict):
                return batch
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return out

    def get_one(self, path: str, **params) -> dict:
        r = self._request("GET", path, params=params)
        if r.status_code == 404:
            return None  # type: ignore[return-value]
        r.raise_for_status()
        return r.json()

    def post(self, path: str, **kwargs) -> requests.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> requests.Response:
        return self._request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs) -> requests.Response:
        return self._request("DELETE", path, **kwargs)

    def patch(self, path: str, **kwargs) -> requests.Response:
        return self._request("PATCH", path, **kwargs)


# ---------------------------------------------------------------------------
# Reconciliation helpers
# ---------------------------------------------------------------------------

class reconciler:
    """Tracks changes across a run."""

    def __init__(self):
        self.changes: list[str] = []
        self.ok: list[str] = []
        self.warnings: list[str] = []

    def changed(self, msg: str):
        self.changes.append(msg)
        print(f"  CHANGED: {msg}")

    def unchanged(self, msg: str):
        self.ok.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)
        print(f"  WARNING: {msg}")


def ensure_groups(api: GitLabAPI, rec: reconciler, apply: bool) -> dict[str, dict]:
    """Ensure the group tree exists with correct descriptions. Return full_path->group dict."""
    print("\n=== Groups ===")
    existing = {g["full_path"]: g for g in api.get("/groups")}
    # Also index by id for parent lookups
    by_id = {g["id"]: g for g in existing.values()}

    for full_path, spec in GROUP_TREE.items():
        if full_path not in existing:
            parent_path = spec["parent"]
            parent_id = None
            if parent_path:
                pg = existing.get(parent_path)
                if not pg:
                    rec.warn(f"Cannot create {full_path}: parent {parent_path} not found")
                    continue
                parent_id = pg["id"]
            if apply:
                payload: dict[str, Any] = {
                    "name": full_path.rsplit("/", 1)[-1].replace("-", " ").title(),
                    "path": full_path.rsplit("/", 1)[-1],
                    "description": spec["description"],
                    "visibility": "private",
                }
                if parent_id:
                    payload["parent_id"] = parent_id
                r = api.post("/groups", json=payload)
                if r.status_code == 201:
                    g = r.json()
                    existing[full_path] = g
                    by_id[g["id"]] = g
                    rec.changed(f"Created group {full_path} (id={g['id']})")
                else:
                    rec.warn(f"Failed to create group {full_path}: {r.status_code} {r.text[:200]}")
            else:
                rec.changed(f"Would create group {full_path}")
        else:
            g = existing[full_path]
            desc = g.get("description") or ""
            if desc != spec["description"]:
                if apply:
                    r = api.put(f"/groups/{g['id']}", json={"description": spec["description"]})
                    if r.ok:
                        rec.changed(f"Updated description for {full_path}")
                    else:
                        rec.warn(f"Failed to update description for {full_path}: {r.status_code}")
                else:
                    rec.changed(f"Would update description for {full_path}")
            else:
                rec.unchanged(f"Group {full_path} OK")
    return existing


def ensure_group_memberships(
    api: GitLabAPI,
    rec: reconciler,
    apply: bool,
    groups: dict[str, dict],
    user_lookup: dict[str, int],
) -> None:
    """Ensure ops users and bots have the correct access on relevant groups."""
    print("\n=== Group Memberships ===")

    for username, uid in user_lookup.items():
        for full_path, gdata in groups.items():
            if full_path not in GROUP_TREE:
                continue
            # Determine the desired level for this user on this group.
            desired = _desired_group_level(username, full_path)
            if desired is None:
                continue

            gid = gdata["id"]
            # Check inherited (all) membership first — if inherited from parent
            # at same/higher level, direct membership may not be needed.
            members = api.get(f"/groups/{gid}/members/all")
            current = next((m for m in members if m["username"] == username), None)
            current_level = current["access_level"] if current else None

            if current_level == desired:
                rec.unchanged(f"{username} on {full_path}: {LEVEL_NAMES[desired]}")
                continue

            # Check if inherited from a parent at >= desired level
            if current_level is not None and current_level >= desired:
                rec.unchanged(
                    f"{username} on {full_path}: inherited {LEVEL_NAMES[current_level]} "
                    f"(>= desired {LEVEL_NAMES[desired]})"
                )
                continue

            # Need to add or update direct membership
            direct = api.get_one(f"/groups/{gid}/members/{uid}")
            if apply:
                if direct is None:
                    r = api.post(
                        f"/groups/{gid}/members",
                        json={"user_id": uid, "access_level": desired},
                    )
                    if r.ok:
                        rec.changed(f"Added {username} as {LEVEL_NAMES[desired]} on {full_path}")
                    else:
                        rec.warn(f"Failed to add {username} to {full_path}: {r.status_code} {r.text[:150]}")
                else:
                    r = api.put(
                        f"/groups/{gid}/members/{uid}",
                        json={"access_level": desired},
                    )
                    if r.ok:
                        rec.changed(f"Updated {username} to {LEVEL_NAMES[desired]} on {full_path}")
                    else:
                        rec.warn(f"Failed to update {username} on {full_path}: {r.status_code} {r.text[:150]}")
            else:
                action = "add" if direct is None else "update"
                rec.changed(f"Would {action} {username} -> {LEVEL_NAMES[desired]} on {full_path}")


def _desired_group_level(username: str, group_path: str) -> int | None:
    """Return desired access level for a user/bot on a group, or None if N/A."""
    # Ops users: Maintainer on all managed groups
    if username in OPS_USERS:
        return MAINTAINER

    # Developer users: class-based least-privilege
    if username in DEVELOPER_USERS:
        for cls, groups_in_cls in GROUPS_BY_CLASS.items():
            if group_path in groups_in_cls:
                return ROLE_MATRIX[cls].get("developers")
        return None

    # Bots: only on groups whose class matches
    for bot_name, bot_spec in BOTS.items():
        if username != bot_name:
            continue
        for cls in bot_spec["applies_to_classes"]:
            if group_path in GROUPS_BY_CLASS.get(cls, []):
                level = ROLE_MATRIX[cls].get(bot_name)
                return level
    return None


def ensure_bots(
    api: GitLabAPI,
    rec: reconciler,
    apply: bool,
    tokens_dir: Path,
    pat_expiry_days: int,
) -> dict[str, int]:
    """Create bot users and PATs. Return username->user_id."""
    print("\n=== Bot Accounts ===")
    result: dict[str, int] = {}

    for bot_name, spec in BOTS.items():
        # Check if user exists
        users = api.get("/users", username=bot_name)
        if users:
            uid = users[0]["id"]
            result[bot_name] = uid
            rec.unchanged(f"Bot user {bot_name} exists (id={uid})")
        else:
            if apply:
                r = api.post(
                    "/users",
                    json={
                        "email": spec["email"],
                        "username": bot_name,
                        "name": spec["name"],
                        "skip_confirmation": True,
                        "force_random_password": True,
                        "reset_password": False,
                        "private_profile": True,
                        "projects_limit": 0,
                    },
                )
                if r.status_code == 201:
                    uid = r.json()["id"]
                    result[bot_name] = uid
                    rec.changed(f"Created bot user {bot_name} (id={uid})")
                else:
                    rec.warn(f"Failed to create {bot_name}: {r.status_code} {r.text[:200]}")
                    continue
            else:
                rec.changed(f"Would create bot user {bot_name}")
                continue

        # PAT management
        existing_pats = api.get("/personal_access_tokens", user_id=uid)
        active_pat = next(
            (t for t in existing_pats if t["name"] == spec["pat_name"] and t["active"]),
            None,
        )
        token_file = tokens_dir / spec["token_file"]

        if active_pat and token_file.exists():
            rec.unchanged(
                f"Bot {bot_name} PAT '{spec['pat_name']}' active (id={active_pat['id']}, "
                f"expires={active_pat.get('expires_at')}), token file present"
            )
        elif active_pat and not token_file.exists():
            # PAT exists in GitLab but local file is missing — cannot recover
            # the value. Revoke and create a new one.
            if apply:
                api.delete(f"/personal_access_tokens/{active_pat['id']}")
                rec.warn(f"Revoked orphaned PAT {active_pat['id']} for {bot_name} (no local token file)")
                _create_bot_pat(api, rec, apply, uid, bot_name, spec, tokens_dir, pat_expiry_days)
            else:
                rec.changed(f"Would recreate PAT for {bot_name} (token file missing)")
        else:
            # No active PAT — create one
            _create_bot_pat(api, rec, apply, uid, bot_name, spec, tokens_dir, pat_expiry_days)

    return result


def _create_bot_pat(
    api: GitLabAPI,
    rec: reconciler,
    apply: bool,
    uid: int,
    bot_name: str,
    spec: dict,
    tokens_dir: Path,
    pat_expiry_days: int,
) -> None:
    expiry = (date.today() + timedelta(days=pat_expiry_days)).isoformat()
    token_file = tokens_dir / spec["token_file"]
    if apply:
        r = api.post(
            f"/users/{uid}/personal_access_tokens",
            json={
                "name": spec["pat_name"],
                "scopes": spec["pat_scopes"],
                "expires_at": expiry,
            },
        )
        if r.status_code == 201:
            data = r.json()
            token_value = data["token"]
            tokens_dir.mkdir(parents=True, exist_ok=True)
            token_file.write_text(token_value + "\n")
            os.chmod(token_file, 0o600)
            rec.changed(
                f"Created PAT for {bot_name} (id={data['id']}, scopes={spec['pat_scopes']}, "
                f"expires={expiry}) -> {token_file}"
            )
        else:
            rec.warn(f"Failed to create PAT for {bot_name}: {r.status_code} {r.text[:200]}")
    else:
        rec.changed(f"Would create PAT for {bot_name} (scopes={spec['pat_scopes']}, expires={expiry})")


def ensure_projects(
    api: GitLabAPI,
    rec: reconciler,
    apply: bool,
) -> dict[str, dict]:
    """Verify project placement, set default branch + remove_source_branch."""
    print("\n=== Projects ===")
    # Use the full project representation (not simple=True) so that
    # remove_source_branch_after_merge is included.
    all_projects = {p["path_with_namespace"]: p for p in api.get("/projects")}
    result: dict[str, dict] = {}

    for path, spec in PROJECTS.items():
        if path not in all_projects:
            rec.warn(f"Project {path} NOT FOUND — it may need manual creation or transfer")
            continue
        proj = all_projects[path]
        result[path] = proj
        changes: dict[str, Any] = {}

        if proj.get("default_branch") != "main":
            changes["default_branch"] = "main"
        if not proj.get("remove_source_branch_after_merge"):
            changes["remove_source_branch_after_merge"] = True

        if changes:
            if apply:
                r = api.put(f"/projects/{proj['id']}", json=changes)
                if r.ok:
                    rec.changed(f"Updated {path}: {list(changes.keys())}")
                else:
                    rec.warn(f"Failed to update {path}: {r.status_code} {r.text[:150]}")
            else:
                rec.changed(f"Would update {path}: {list(changes.keys())}")
        else:
            rec.unchanged(f"Project {path} OK")

    # Warn about unexpected projects
    for path in all_projects:
        if path not in PROJECTS:
            rec.warn(f"Unexpected project (not in spec): {path}")

    return result


def ensure_branch_protection(
    api: GitLabAPI,
    rec: reconciler,
    apply: bool,
    projects: dict[str, dict],
) -> None:
    """Ensure main branch protection per project class policy."""
    print("\n=== Branch Protection ===")

    for path, proj in projects.items():
        pclass = PROJECTS[path]["class"]
        policy = BRANCH_PROTECTION[pclass]
        pid = proj["id"]

        protected = api.get(f"/projects/{pid}/protected_branches")
        main_pb = next((b for b in protected if b["name"] == "main"), None)

        desired_push = policy["push"]
        desired_merge = policy["merge"]

        if main_pb:
            current_push = _extract_level(main_pb.get("push_access_levels"))
            current_merge = _extract_level(main_pb.get("merge_access_levels"))

            if current_push == desired_push and current_merge == desired_merge:
                rec.unchanged(
                    f"{path}: main protected (push={LEVEL_NAMES.get(desired_push, desired_push)}, "
                    f"merge={LEVEL_NAMES[desired_merge]})"
                )
                continue

            # PATCH on protected branches is a silent no-op on this GitLab
            # version (19.1.2), so the reliable path is delete + recreate.
            # The window is small and the branch itself is unaffected.
            if apply:
                api.delete(f"/projects/{pid}/protected_branches/main")
                _protect_branch(api, rec, pid, path, desired_push, desired_merge)
            else:
                rec.changed(
                    f"Would update {path} protection: "
                    f"push {LEVEL_NAMES.get(current_push,'?')}->{LEVEL_NAMES.get(desired_push,desired_push)}, "
                    f"merge {LEVEL_NAMES.get(current_merge,'?')}->{LEVEL_NAMES[desired_merge]}"
                )
        else:
            if apply:
                _protect_branch(api, rec, pid, path, desired_push, desired_merge)
            else:
                rec.changed(
                    f"Would protect {path} main: push={LEVEL_NAMES.get(desired_push,desired_push)}, "
                    f"merge={LEVEL_NAMES[desired_merge]}"
                )


def _extract_level(access_levels: list[dict] | None) -> int | None:
    """Extract the access_level from a protected-branch access-levels array."""
    if not access_levels:
        return None
    # Use the first non-user, non-deploy-key entry
    for entry in access_levels:
        if entry.get("access_level") is not None:
            return entry["access_level"]
    return None


def _protect_branch(
    api: GitLabAPI,
    rec: reconciler,
    pid: int,
    path: str,
    push_level: int,
    merge_level: int,
) -> None:
    r = api.post(
        f"/projects/{pid}/protected_branches",
        json={
            "name": "main",
            "push_access_level": push_level,
            "merge_access_level": merge_level,
            "allow_force_push": False,
        },
    )
    if r.ok:
        rec.changed(
            f"Protected {path} main: push={LEVEL_NAMES.get(push_level, push_level)}, "
            f"merge={LEVEL_NAMES[merge_level]}"
        )
    else:
        rec.warn(f"Failed to protect {path} main: {r.status_code} {r.text[:200]}")


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------

def verify_state(api: GitLabAPI, expected_bots: dict[str, int]) -> bool:
    """Read-only verification. Return True if converged."""
    print("\n" + "=" * 70)
    print("VERIFICATION")
    print("=" * 70)
    ok = True

    # Groups
    print("\n--- Groups ---")
    groups = {g["full_path"]: g for g in api.get("/groups")}
    for full_path, spec in GROUP_TREE.items():
        if full_path not in groups:
            print(f"  MISSING: group {full_path}")
            ok = False
        elif groups[full_path].get("description") != spec["description"]:
            print(f"  DRIFT: {full_path} description mismatch")
            ok = False
        else:
            print(f"  OK: {full_path}")

    # Projects
    print("\n--- Projects ---")
    projects = {p["path_with_namespace"]: p for p in api.get("/projects")}
    for path, spec in PROJECTS.items():
        if path not in projects:
            print(f"  MISSING: {path}")
            ok = False
            continue
        p = projects[path]
        issues = []
        if p.get("default_branch") != "main":
            issues.append(f"default_branch={p.get('default_branch')}")
        if not p.get("remove_source_branch_after_merge"):
            issues.append("remove_source_branch=False")
        if issues:
            print(f"  DRIFT: {path}: {', '.join(issues)}")
            ok = False
        else:
            print(f"  OK: {path}")

    # Branch protection
    print("\n--- Branch Protection ---")
    for path, spec in PROJECTS.items():
        if path not in projects:
            continue
        pclass = spec["class"]
        policy = BRANCH_PROTECTION[pclass]
        pid = projects[path]["id"]
        protected = api.get(f"/projects/{pid}/protected_branches")
        main_pb = next((b for b in protected if b["name"] == "main"), None)
        if not main_pb:
            print(f"  MISSING: {path} main not protected")
            ok = False
            continue
        push = _extract_level(main_pb.get("push_access_levels"))
        merge = _extract_level(main_pb.get("merge_access_levels"))
        if push != policy["push"] or merge != policy["merge"]:
            print(
                f"  DRIFT: {path}: push={push}({LEVEL_NAMES.get(push)}), merge={merge}({LEVEL_NAMES.get(merge)}) "
                f"!= expected push={policy['push']}({LEVEL_NAMES.get(policy['push'])}), "
                f"merge={policy['merge']}({LEVEL_NAMES[policy['merge']]})"
            )
            ok = False
        else:
            label = " [MR-only]" if pclass == "gitops" else ""
            print(f"  OK: {path} push={LEVEL_NAMES.get(push, push)}, merge={LEVEL_NAMES[merge]}{label}")

    # Bot users + PATs
    print("\n--- Bot Accounts ---")
    for bot_name, spec in BOTS.items():
        users = api.get("/users", username=bot_name)
        if not users:
            print(f"  MISSING: bot user {bot_name}")
            ok = False
            continue
        uid = users[0]["id"]
        pats = api.get("/personal_access_tokens", user_id=uid)
        active = [t for t in pats if t["name"] == spec["pat_name"] and t["active"]]
        if not active:
            print(f"  MISSING: {bot_name} has no active PAT '{spec['pat_name']}'")
            ok = False
        else:
            t = active[0]
            scopes_ok = set(t["scopes"]) == set(spec["pat_scopes"])
            token_file = Path(os.environ.get("GITLAB_TOKENS_DIR", DEFAULT_TOKENS_DIR)) / spec["token_file"]
            file_ok = token_file.exists()
            status = "OK" if (scopes_ok and file_ok) else "DRIFT"
            if status == "DRIFT":
                ok = False
            print(
                f"  {status}: {bot_name} (id={uid}) PAT id={t['id']} scopes={t['scopes']} "
                f"active={t['active']} expires={t.get('expires_at')} token_file={file_ok}"
            )

    # Group memberships for bots
    print("\n--- Bot Group Memberships ---")
    for bot_name, spec in BOTS.items():
        users = api.get("/users", username=bot_name)
        if not users:
            continue
        uid = users[0]["id"]
        for cls in spec["applies_to_classes"]:
            desired_level = ROLE_MATRIX[cls][bot_name]
            for gpath in GROUPS_BY_CLASS.get(cls, []):
                gdata = groups.get(gpath)
                if not gdata:
                    continue
                members = api.get(f"/groups/{gdata['id']}/members/all")
                m = next((x for x in members if x["username"] == bot_name), None)
                actual = m["access_level"] if m else None
                if actual is not None and actual >= desired_level:
                    print(f"  OK: {bot_name} on {gpath}: {LEVEL_NAMES.get(actual)} (>= {LEVEL_NAMES[desired_level]})")
                else:
                    print(f"  DRIFT: {bot_name} on {gpath}: level={actual} (expected >={desired_level})")
                    ok = False

    return ok


# ---------------------------------------------------------------------------
# Manifest generation
# ---------------------------------------------------------------------------

def generate_manifest(api: GitLabAPI, path: Path) -> None:
    """Write a human-readable manifest of the ACL state."""
    groups = sorted(api.get("/groups"), key=lambda g: g["full_path"])
    projects = sorted(api.get("/projects"), key=lambda p: p["path_with_namespace"])

    lines: list[str] = []
    lines.append("# GitLab ACL Manifest\n")
    lines.append("> Auto-generated by `scripts/gitlab-acls.py`. Do not edit manually.\n")
    lines.append(f"> Last generated: {date.today().isoformat()}\n\n")

    lines.append("## Role Matrix\n\n")
    lines.append("| Project class | Developers | Ops | deploy-bot | app-ci-bot | Branch push | Branch merge |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for cls in ["apps", "gitops", "infra", "legacy"]:
        rm = ROLE_MATRIX[cls]
        bp = BRANCH_PROTECTION[cls]
        lines.append(
            f"| {cls} | {LEVEL_NAMES.get(rm['developers'], 'N/A')} | "
            f"{LEVEL_NAMES[rm['ops']]} | "
            f"{LEVEL_NAMES.get(rm['deploy-bot'], 'N/A')} | "
            f"{LEVEL_NAMES.get(rm['app-ci-bot'], 'N/A')} | "
            f"{LEVEL_NAMES.get(bp['push'], bp['push'])} | "
            f"{LEVEL_NAMES[bp['merge']]} |"
        )

    lines.append("\n## Groups\n\n")
    lines.append("| ID | Full path | Description |")
    lines.append("| --- | --- | --- |")
    for g in groups:
        desc = (g.get("description") or "").replace("|", "\\|")
        lines.append(f"| {g['id']} | `{g['full_path']}` | {desc} |")

    lines.append("\n## Projects\n\n")
    lines.append("| ID | Path | Class | Default branch | Remove source branch | Notes |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for p in projects:
        pwn = p["path_with_namespace"]
        spec = PROJECTS.get(pwn, {})
        pclass = spec.get("class", "?")
        note = spec.get("note", "")
        rsb = p.get("remove_source_branch_after_merge")
        lines.append(
            f"| {p['id']} | `{pwn}` | {pclass} | {p.get('default_branch')} | "
            f"{'yes' if rsb else 'no'} | {note} |"
        )

    lines.append("\n## Bot Accounts\n\n")
    lines.append("| Username | Name | PAT scopes | Groups (level) | Token file |")
    lines.append("| --- | --- | --- | --- | --- |")
    for bot_name, spec in BOTS.items():
        users = api.get("/users", username=bot_name)
        groups_str = ""
        if users:
            uid = users[0]["id"]
            parts = []
            for cls in spec["applies_to_classes"]:
                lvl = ROLE_MATRIX[cls][bot_name]
                for gp in GROUPS_BY_CLASS.get(cls, []):
                    parts.append(f"{gp} ({LEVEL_NAMES[lvl]})")
            groups_str = ", ".join(parts)
        lines.append(
            f"| `{bot_name}` | {spec['name']} | {', '.join(spec['pat_scopes'])} | "
            f"{groups_str} | `{spec['token_file']}` |"
        )

    lines.append("\n## Known Limitations (GitLab CE)\n\n")
    lines.append(
        "1. **MR approval rules** (`approvals_before_merge`, approval rules) are a "
        "**Premium** feature. This CE instance does not support them. The gitops "
        "\"merge only via MR\" requirement is enforced through branch protection: "
        "`push_access_level=0` (No one) on `main`, so all changes must go through a "
        "merge request. Enforcing a specific number of approvals requires a Premium "
        "license.\n"
    )
    lines.append(
        "2. **`fun/fun-games` (id=1)** is a legacy repo outside the convention. It is "
        "kept as-is per `docs/KNOWN-DEBT.md`. Active development uses "
        "`fun/development/fun-games` (id=2).\n"
    )
    lines.append(
        "3. **`fun/argocd/ansbile-k8s-full-setup-n0xeid` (id=3)** has a path typo "
        "(`ansbile`). It is kept as-is to avoid breaking Argo CD source URLs. See "
        "`docs/KNOWN-DEBT.md`.\n"
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    print(f"\nManifest written to {path}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Provision GitLab ACLs to spec")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry run)")
    parser.add_argument("--verify", action="store_true", help="Verify only, exit non-zero on drift")
    parser.add_argument(
        "--manifest",
        type=str,
        default="docs/GITLAB_ACL_MANIFEST.md",
        help="Path to write the manifest (relative to repo root)",
    )
    args = parser.parse_args()

    token = os.environ.get("GITLAB_TOKEN", "").strip()
    if not token:
        sys.stderr.write("ERROR: GITLAB_TOKEN environment variable is required.\n")
        return 2
    base_url = os.environ.get("GITLAB_URL", DEFAULT_GITLAB_URL)
    tokens_dir = Path(os.environ.get("GITLAB_TOKENS_DIR", DEFAULT_TOKENS_DIR))
    pat_expiry_days = int(os.environ.get("PAT_EXPIRY_DAYS", str(DEFAULT_PAT_EXPIRY_DAYS)))

    api = GitLabAPI(base_url, token)

    # Sanity check
    r = api.get_one("/version")
    if r is None:
        sys.stderr.write(f"ERROR: Cannot reach GitLab at {base_url}\n")
        return 2
    print(f"GitLab {r.get('version')} at {base_url}")
    user = api.get_one("/user")
    if user:
        print(f"Authenticated as: {user['username']} (admin={user.get('is_admin')})")
        if not user.get("is_admin"):
            sys.stderr.write("WARNING: Token user is not admin — some operations may fail.\n")

    repo_root = Path(__file__).resolve().parents[1]

    if args.verify:
        ok = verify_state(api, {})
        if ok:
            print("\nVERIFICATION PASSED: all resources match spec.\n")
            generate_manifest(api, repo_root / args.manifest)
            return 0
        else:
            print("\nVERIFICATION FAILED: drift detected. See above.\n")
            return 1

    apply = args.apply
    mode_label = "APPLY" if apply else "DRY RUN"
    print(f"\nMode: {mode_label}\n")

    rec = reconciler()

    # 1. Groups
    groups = ensure_groups(api, rec, apply)

    # 2. Bot users + PATs
    bot_ids = ensure_bots(api, rec, apply, tokens_dir, pat_expiry_days)

    # 3. Build full user lookup (bots + ops + developers)
    user_lookup: dict[str, int] = {}
    for uname in OPS_USERS + DEVELOPER_USERS:
        users = api.get("/users", username=uname)
        if users:
            user_lookup[uname] = users[0]["id"]
        else:
            rec.warn(f"Configured user {uname} not found in GitLab — skipped")
    user_lookup.update(bot_ids)

    # 4. Group memberships
    ensure_group_memberships(api, rec, apply, groups, user_lookup)

    # 5. Projects (default branch, remove source branch)
    projects = ensure_projects(api, rec, apply)

    # 6. Branch protection
    ensure_branch_protection(api, rec, apply, projects)

    # 7. Manifest
    if apply:
        generate_manifest(api, repo_root / args.manifest)

    # Summary
    print("\n" + "=" * 70)
    print(f"SUMMARY ({mode_label})")
    print(f"  Changes:     {len(rec.changes)}")
    print(f"  Already OK:  {len(rec.ok)}")
    print(f"  Warnings:    {len(rec.warnings)}")
    if rec.warnings:
        print("\n  Warnings:")
        for w in rec.warnings:
            print(f"    - {w}")
    print("=" * 70)

    if not apply and rec.changes:
        print("\nDry run complete. Run with --apply to make changes.")
    elif apply:
        print("\nApply complete. Running verification...")
        ok = verify_state(api, bot_ids)
        if ok:
            print("\nAll checks passed.\n")
        else:
            print("\nSome checks failed — review warnings above.\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
