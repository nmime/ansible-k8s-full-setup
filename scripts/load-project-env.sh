#!/usr/bin/env bash
# Safely load the repository-local, gitignored .env into operational scripts.

_load_project_env() {
  local loader_dir project_root env_file mode line key value first last

  loader_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  project_root="$(cd "${loader_dir}/.." && pwd)"
  env_file="${PROJECT_ENV_FILE:-${project_root}/.env}"

  [[ -e "$env_file" ]] || return 0
  if [[ -L "$env_file" || ! -f "$env_file" ]]; then
    printf 'ERROR: project environment must be a regular, non-symlink file: %s\n' "$env_file" >&2
    return 1
  fi

  if mode=$(stat -f '%Lp' "$env_file" 2>/dev/null); then
    :
  else
    mode=$(stat -c '%a' "$env_file")
  fi
  if [[ "${mode: -2}" != "00" ]]; then
    printf 'ERROR: project environment must not be readable or writable by group/others: %s (mode %s)\n' \
      "$env_file" "$mode" >&2
    return 1
  fi

  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$line" || "${line:0:1}" == "#" ]] && continue

    if [[ ! "$line" =~ ^(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]]; then
      printf 'ERROR: invalid assignment in project environment %s\n' "$env_file" >&2
      return 1
    fi
    key="${BASH_REMATCH[2]}"
    value="${BASH_REMATCH[3]}"
    value="${value%"${value##*[![:space:]]}"}"

    if [[ -n "$value" ]]; then
      first="${value:0:1}"
      last="${value:$((${#value} - 1)):1}"
      if [[ "$first" == '"' || "$first" == "'" ]]; then
        if [[ "$last" != "$first" || ${#value} -lt 2 ]]; then
          printf 'ERROR: unmatched quote for %s in project environment %s\n' "$key" "$env_file" >&2
          return 1
        fi
        value="${value:1:$((${#value} - 2))}"
      fi
    fi

    # Explicit process environment always wins over repository-local defaults.
    if [[ -z "${!key+x}" ]]; then
      printf -v "$key" '%s' "$value"
      # shellcheck disable=SC2163 # key is intentionally the variable name.
      export "$key"
    fi
  done < "$env_file"

  PROJECT_ENV_LOADED="$env_file"
  export PROJECT_ENV_LOADED
}

if _load_project_env; then
  unset -f _load_project_env
else
  _project_env_status=$?
  unset -f _load_project_env
  return "$_project_env_status"
fi
