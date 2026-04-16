#!/bin/bash
set -e

ADMIN_USER="${FORGEJO_ADMIN_USER:-gitadmin}"
ADMIN_PASSWORD="${FORGEJO_ADMIN_PASSWORD:-admin123}"
ADMIN_EMAIL="${FORGEJO_ADMIN_EMAIL:-admin@localhost}"
TOKEN_NAME="proxy-token"
TOKEN_FILE="/data/gitea/proxy-token"

# Forward SIGTERM/SIGINT to child processes
trap 'kill -TERM $FORGEJO_PID $PROXY_PID 2>/dev/null; wait $FORGEJO_PID $PROXY_PID' TERM INT

# First start: set up directories and copy config
if [ ! -f /data/gitea/gitea.db ]; then
  mkdir -p /data/gitea/conf /data/git /data/ssh
  chown -R git:git /data
  cp /etc/forgejo/app.ini /data/gitea/conf/app.ini
  chown git:git /data/gitea/conf/app.ini
fi

# Start Forgejo as user git in background
/sbin/su-exec git /usr/local/bin/gitea web --config /data/gitea/conf/app.ini &
FORGEJO_PID=$!

# Wait for Forgejo HTTP to be ready
echo "Waiting for Forgejo to start..."
READY=0
for i in $(seq 1 30); do
  if curl -sf http://127.0.0.1:3000/api/healthz > /dev/null 2>&1; then
    READY=1
    echo "Forgejo is ready."
    break
  fi
  if ! kill -0 $FORGEJO_PID 2>/dev/null; then
    echo "Forgejo process died unexpectedly."
    exit 1
  fi
  sleep 1
done

if [ "$READY" -eq 0 ]; then
  echo "Forgejo did not start within 30 seconds."
  kill -TERM $FORGEJO_PID 2>/dev/null
  exit 1
fi

# Create admin user if not exists
if ! curl -sf -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
    "http://127.0.0.1:3000/api/v1/user" > /dev/null 2>&1; then
  echo "Creating admin user '${ADMIN_USER}'..."
  /sbin/su-exec git /usr/local/bin/gitea admin user create \
    --config /data/gitea/conf/app.ini \
    --username "${ADMIN_USER}" \
    --password "${ADMIN_PASSWORD}" \
    --email "${ADMIN_EMAIL}" \
    --admin \
    --must-change-password=false
fi

# Create API token if not already saved
if [ ! -f "${TOKEN_FILE}" ]; then
  echo "Creating API token '${TOKEN_NAME}'..."
  TOKEN=$(curl -sf -X POST "http://127.0.0.1:3000/api/v1/users/${ADMIN_USER}/tokens" \
    -H "Content-Type: application/json" \
    -u "${ADMIN_USER}:${ADMIN_PASSWORD}" \
    -d "{\"name\": \"${TOKEN_NAME}\", \"scopes\": [\"write:repository\", \"read:user\"]}" \
    | sed -n 's/.*"sha1":"\([^"]*\)".*/\1/p')
  echo "$TOKEN" > "${TOKEN_FILE}"
  chmod 600 "${TOKEN_FILE}"
  chown git:git "${TOKEN_FILE}"
fi

# Start git-proxy
FORGEJO_TOKEN="$(cat ${TOKEN_FILE})" \
FORGEJO_URL="http://127.0.0.1:3000" \
FORGEJO_USER="${ADMIN_USER}" \
FORGEJO_PASSWORD="${ADMIN_PASSWORD}" \
PROXY_PORT="${PROXY_PORT:-8080}" \
PROXY_CONFIG="${PROXY_CONFIG:-/config/hooks.yml}" \
  python3 /app/proxy.py &
PROXY_PID=$!

echo "---"
echo "Forgejo:   http://0.0.0.0:3000  (admin: ${ADMIN_USER})"
echo "git-proxy: http://0.0.0.0:${PROXY_PORT:-8080}"
echo "---"

wait $FORGEJO_PID $PROXY_PID
