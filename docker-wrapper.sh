#!/bin/bash
# Docker wrapper: automatically injects GIT_CONFIG_* variables into every
# `docker run` call. Exits with an error if the caller already passes any
# of the injected variables, to prevent silent conflicts.

REAL_DOCKER=/usr/bin/docker

INJECT_VARS=(
  GIT_CONFIG_COUNT
  GIT_CONFIG_KEY_0
  GIT_CONFIG_VALUE_0
)

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
