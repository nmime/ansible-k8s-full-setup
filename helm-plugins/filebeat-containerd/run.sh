#!/bin/sh
set -eu

plugin_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
exec "$plugin_dir/../../scripts/filebeat-containerd-post-renderer.sh"
