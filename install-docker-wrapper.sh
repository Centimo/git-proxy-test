#!/bin/bash
# Installs docker-wrapper.sh as /usr/local/bin/docker.
# Requires write access to /usr/local/bin (run as root or via sudo).

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WRAPPER_SRC="$SCRIPT_DIR/docker-wrapper.sh"
INSTALL_PATH="/usr/local/bin/docker"

if [[ ! -f "$WRAPPER_SRC" ]]; then
  echo "ERROR: wrapper not found at $WRAPPER_SRC" >&2
  exit 1
fi

cp "$WRAPPER_SRC" "$INSTALL_PATH"
chmod +x "$INSTALL_PATH"

echo "Installed docker wrapper to $INSTALL_PATH"
echo "Real docker: $(/usr/bin/docker --version)"
echo "Wrapper test: $(docker --version)"
