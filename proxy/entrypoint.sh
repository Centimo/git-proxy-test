#!/bin/sh
set -e

echo "Waiting for Forgejo token..."
for i in $(seq 1 30); do
  TOKEN=$(cat /forgejo-data/gitea/proxy-token 2>/dev/null)
  if [ -n "$TOKEN" ]; then
    echo "Token loaded."
    export FORGEJO_TOKEN="$TOKEN"
    break
  fi
  sleep 1
done

if [ -z "$FORGEJO_TOKEN" ]; then
  echo "ERROR: Forgejo token not found after 30 seconds."
  exit 1
fi

exec python3 /app/proxy.py
