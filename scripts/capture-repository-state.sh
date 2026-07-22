#!/usr/bin/env bash
# Capture the exact tracked and safe non-ignored source state for DR recovery.
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "Usage: capture-repository-state.sh REPOSITORY DESTINATION" >&2
  exit 2
}

repository=$(cd "$1" && pwd)
destination=$2
mkdir -p "$destination"
destination=$(cd "$destination" && pwd)

git -C "$repository" rev-parse --is-inside-work-tree >/dev/null
git -C "$repository" bundle create "$destination/repository.bundle" HEAD
git -C "$repository" status --porcelain=v1 > "$destination/git-status.txt"
# Diffing against HEAD captures the final working-tree view of both staged and
# unstaged tracked changes. Plain `git diff` silently omits staged changes.
git -C "$repository" diff HEAD --binary > "$destination/worktree.patch"
git -C "$repository" rev-parse HEAD > "$destination/git-revision.txt"

untracked_nul=$(mktemp)
cleanup() { rm -f "$untracked_nul"; }
trap cleanup EXIT INT TERM
git -C "$repository" ls-files --others --exclude-standard -z > "$untracked_nul"
: > "$destination/repository-untracked-files.txt"
untracked_count=0
while IFS= read -r -d '' path; do
  # Git supplies repository-relative paths. Reject unusual traversal/newline
  # names and common credential/key material rather than putting them in even
  # an encrypted recovery archive. Generated platform secrets are captured by
  # the dedicated, permission-restricted bundle paths instead.
  [[ "$path" != /* && "$path" != . && "$path" != ./* && "$path" != ../* \
    && "$path" != */../* && "$path" != */./* && "$path" != *//* \
    && "$path" != *$'\n'* ]] || {
    echo "Unsafe untracked path cannot be captured: $path" >&2
    exit 1
  }
  # The recovery format contains file contents only. A symlink can redirect a
  # later extraction, while FIFOs/devices/sockets can block or access resources
  # outside the repository. Nested ordinary files remain fully supported.
  [[ ! -L "$repository/$path" && -f "$repository/$path" ]] || {
    echo "Refusing to capture non-regular untracked file: $path" >&2
    exit 1
  }
  case "$path" in
    .env|.env.*|*/.env|*/.env.*|*.pem|*.key|*.p12|*.pfx|*.jks|*.keystore|\
    .npmrc|*/.npmrc|.pypirc|*/.pypirc|.netrc|*/.netrc|\
    .aws/credentials|*/.aws/credentials|.docker/config.json|*/.docker/config.json|\
    .kube/config|*/.kube/config|credentials|*/credentials|\
    credentials.json|*/credentials.json|*-credentials.json|*_credentials.json|\
    *service-account.json|*service_account.json|\
    *.tfvars|*.tfvars.json|*.ovpn|\
    id_rsa|*/id_rsa|id_rsa.*|*/id_rsa.*|id_dsa|*/id_dsa|id_dsa.*|*/id_dsa.*|\
    id_ecdsa|*/id_ecdsa|id_ecdsa.*|*/id_ecdsa.*|id_ed25519|*/id_ed25519|\
    id_ed25519.*|*/id_ed25519.*|*kubeconfig*|*vault-password*|\
    playbooks/.platform-secrets.yml|playbooks/.vault-init-*.json)
      echo "Refusing to capture credential-like untracked file: $path" >&2
      exit 1
      ;;
  esac
  if git -C "$repository" check-ignore --quiet -- "$path"; then
    echo "Refusing to capture ignored file returned by source inventory: $path" >&2
    exit 1
  fi
  printf '%s\n' "$path" >> "$destination/repository-untracked-files.txt"
  untracked_count=$((untracked_count + 1))
done < "$untracked_nul"

# --null preserves spaces and shell metacharacters without evaluation. Every
# source inventory member was already proven to be a regular, non-link file.
COPYFILE_DISABLE=1 tar -C "$repository" --null -T "$untracked_nul" -cf \
  "$destination/repository-untracked.tar"
printf '%s\n' "$untracked_count" > "$destination/repository-untracked-count.txt"
