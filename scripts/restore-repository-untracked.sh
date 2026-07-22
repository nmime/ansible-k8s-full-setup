#!/usr/bin/env bash
# Safely replay captured untracked regular files into an existing Git checkout.
set -euo pipefail

[[ $# -eq 2 ]] || {
  echo "Usage: restore-repository-untracked.sh OPERATOR_REPOSITORY_STATE CHECKOUT" >&2
  exit 2
}

state_dir=$1
checkout=$2
for required in repository-untracked.tar repository-untracked-files.txt \
  repository-untracked-count.txt; do
  [[ -f "$state_dir/$required" ]] || {
    echo "Repository recovery input is missing: $state_dir/$required" >&2
    exit 1
  }
done
[[ -d "$checkout" ]] || {
  echo "Checkout is not a directory: $checkout" >&2
  exit 1
}
command -v python3 >/dev/null || {
  echo "python3 is required for safe repository-state replay" >&2
  exit 1
}
command -v git >/dev/null || {
  echo "git is required for safe repository-state replay" >&2
  exit 1
}

umask 077
python3 - \
  "$state_dir/repository-untracked.tar" \
  "$state_dir/repository-untracked-files.txt" \
  "$state_dir/repository-untracked-count.txt" \
  "$checkout" <<'PY'
import os
import pathlib
import stat
import subprocess
import sys
import tarfile


archive_path, paths_path, count_path, checkout_path = sys.argv[1:]
checkout = pathlib.Path(checkout_path).resolve(strict=True)


def fail(message: str) -> None:
    raise SystemExit(f"Repository untracked replay refused: {message}")


top_level = subprocess.run(
    ["git", "-C", str(checkout), "rev-parse", "--show-toplevel"],
    check=False,
    capture_output=True,
    text=True,
)
if top_level.returncode != 0:
    fail("destination is not a Git checkout")
if pathlib.Path(top_level.stdout.strip()).resolve() != checkout:
    fail("destination must be the Git checkout root")

try:
    expected_count = int(pathlib.Path(count_path).read_text(encoding="utf-8").strip())
except (OSError, ValueError):
    fail("untracked file count is invalid")
if expected_count < 0:
    fail("untracked file count is negative")

try:
    declared = pathlib.Path(paths_path).read_text(encoding="utf-8").splitlines()
except (OSError, UnicodeError):
    fail("untracked path inventory is unreadable UTF-8")
if len(declared) != expected_count:
    fail("untracked path inventory does not match its recorded count")
if len(set(declared)) != len(declared):
    fail("untracked path inventory contains duplicate entries")


def validate_relative_path(name: str) -> pathlib.PurePosixPath:
    if not name or any(character in name for character in ("\x00", "\n", "\r")):
        fail("archive contains an empty or control-character path")
    path = pathlib.PurePosixPath(name)
    if (
        path.is_absolute()
        or ".." in path.parts
        or "." in path.parts
        or str(path) != name
        or any(part == "" for part in path.parts)
    ):
        fail(f"archive contains an unsafe path: {name!r}")
    return path


declared_paths = [validate_relative_path(name) for name in declared]
try:
    source = tarfile.open(archive_path, "r:*")
except (OSError, tarfile.TarError):
    fail("untracked archive is unreadable")

with source:
    members = source.getmembers()
    member_names = [member.name for member in members]
    if member_names != declared:
        fail("archive members do not exactly match the recorded path inventory")
    for member in members:
        validate_relative_path(member.name)
        if not member.isreg():
            fail(f"archive member is not a regular file: {member.name!r}")

    # Refuse every collision before creating any file. The index check protects
    # tracked files even when they are currently deleted from the worktree; the
    # lstat walk rejects existing destinations and symlinked parent components.
    for pure_path in declared_paths:
        name = str(pure_path)
        for prefix_length in range(1, len(pure_path.parts) + 1):
            prefix = "/".join(pure_path.parts[:prefix_length])
            tracked = subprocess.run(
                [
                    "git",
                    "-C",
                    str(checkout),
                    "ls-files",
                    "-z",
                    "--cached",
                    "--",
                    f":(literal){prefix}",
                ],
                check=False,
                capture_output=True,
            )
            if tracked.returncode != 0:
                fail(f"could not inspect tracked destination path: {name!r}")
            if tracked.stdout:
                if prefix == name:
                    fail(f"destination path is tracked: {name!r}")
                fail(f"destination parent is tracked: {prefix!r}")

        current = checkout
        for component in pure_path.parts[:-1]:
            current = current / component
            try:
                metadata = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                fail(f"destination parent is not a real directory: {str(pure_path)!r}")
        try:
            (checkout / pathlib.Path(*pure_path.parts)).lstat()
        except FileNotFoundError:
            pass
        else:
            fail(f"destination path already exists: {name!r}")

    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    root_fd = os.open(checkout, os.O_RDONLY | directory)
    try:
        for member, pure_path in zip(members, declared_paths):
            parent_fd = os.dup(root_fd)
            try:
                for component in pure_path.parts[:-1]:
                    try:
                        next_fd = os.open(
                            component,
                            os.O_RDONLY | directory | nofollow,
                            dir_fd=parent_fd,
                        )
                    except FileNotFoundError:
                        os.mkdir(component, mode=0o700, dir_fd=parent_fd)
                        next_fd = os.open(
                            component,
                            os.O_RDONLY | directory | nofollow,
                            dir_fd=parent_fd,
                        )
                    os.close(parent_fd)
                    parent_fd = next_fd

                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | nofollow
                destination_fd = os.open(
                    pure_path.parts[-1], flags, 0o600, dir_fd=parent_fd
                )
                try:
                    payload = source.extractfile(member)
                    if payload is None:
                        fail(f"could not read archive member: {member.name!r}")
                    with payload:
                        while True:
                            chunk = payload.read(1024 * 1024)
                            if not chunk:
                                break
                            remaining = memoryview(chunk)
                            while remaining:
                                written = os.write(destination_fd, remaining)
                                if written <= 0:
                                    raise OSError("short write while restoring archive member")
                                remaining = remaining[written:]
                    os.fchmod(destination_fd, member.mode & 0o777)
                except BaseException:
                    os.close(destination_fd)
                    destination_fd = -1
                    os.unlink(pure_path.parts[-1], dir_fd=parent_fd)
                    raise
                finally:
                    if destination_fd >= 0:
                        os.close(destination_fd)
            finally:
                os.close(parent_fd)
    finally:
        os.close(root_fd)

print(f"Restored {len(members)} safe untracked file(s) into {checkout}")
PY
