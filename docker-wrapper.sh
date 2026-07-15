#!/bin/bash
# Docker wrapper: automatically injects GIT_CONFIG_* variables into every
# `docker run` call. Exits with an error if the caller already passes any
# of the injected variables, to prevent silent conflicts.
#
# Supports an arbitrary number of git-config pairs: GIT_CONFIG_COUNT=N plus
# GIT_CONFIG_KEY_0..KEY_{N-1} / GIT_CONFIG_VALUE_0..VALUE_{N-1} (one insteadOf
# pair per allowlisted upstream host).

REAL_DOCKER=/usr/bin/docker

# Build the list of variables to forward from GIT_CONFIG_COUNT (default 0).
INJECT_VARS=(GIT_CONFIG_COUNT)
git_config_count="${GIT_CONFIG_COUNT:-0}"
if [[ "$git_config_count" =~ ^[0-9]+$ ]]; then
  for ((idx = 0; idx < git_config_count; idx++)); do
    INJECT_VARS+=("GIT_CONFIG_KEY_${idx}" "GIT_CONFIG_VALUE_${idx}")
  done
fi

if [[ "$1" != "run" ]]; then
  exec "$REAL_DOCKER" "$@"
fi

# Extract variable name from -e/-e=VAR/--env/--env=VAR argument pair or single token
extract_varname() {
  local arg="$1" next="$2"
  local val
  case "$arg" in
    -e|--env)    val="$next" ;;
    -e*)         val="${arg#-e}" ;;
    --env=*)     val="${arg#--env=}" ;;
    *)           return ;;
  esac
  echo "${val%%=*}"
}

# Check for conflicts
i=1
while [[ $i -le $# ]]; do
  varname=$(extract_varname "${!i}" "${@:$((i+1)):1}")
  if [[ -n "$varname" ]]; then
    for inject in "${INJECT_VARS[@]}"; do
      if [[ "$varname" == "$inject" ]]; then
        echo "docker-wrapper ERROR: conflicting variable '$inject' already passed to docker run" >&2
        exit 1
      fi
    done
  fi
  ((i++))
done

# Inject flags for variables present in the current environment
inject_flags=()
for var in "${INJECT_VARS[@]}"; do
  [[ -v "$var" ]] && inject_flags+=(-e "$var")
done

exec "$REAL_DOCKER" run "${inject_flags[@]}" "${@:2}"
