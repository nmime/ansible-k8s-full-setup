#!/bin/sh
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
project_python="$script_dir/../.venv/bin/python3"

if [ -x "$project_python" ]; then
  exec "$project_python" "$script_dir/filebeat-containerd-post-renderer.py"
fi

exec python3 "$script_dir/filebeat-containerd-post-renderer.py"
