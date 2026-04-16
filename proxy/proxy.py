#!/usr/bin/env python3
"""
git-proxy: HTTP proxy that mirrors GitHub repositories into Forgejo on demand.

Client configures:
  git config --global url."http://<proxy-host>:8080/".insteadOf "https://github.com/"

Then `git clone https://github.com/owner/repo` becomes
     `git clone http://<proxy-host>:8080/owner/repo`

Proxy behaviour:
  - Mirror exists and is synced  → proxy the request to Forgejo
  - Mirror exists but empty      → return git ERR (retry)
  - Mirror does not exist        → create mirror, return git ERR (retry)
"""

import base64
import logging
import os
import re
import threading
import time
import urllib.request
import urllib.error
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn

from config import load_config
import hooks as _hooks

FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://127.0.0.1:3000")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")
FORGEJO_USER = os.environ.get("FORGEJO_USER", "gitadmin")
FORGEJO_PASSWORD = os.environ.get("FORGEJO_PASSWORD", "")
GITHUB_BASE = "https://github.com"
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8080"))
FORGEJO_API_TIMEOUT = 30
FORGEJO_MIGRATE_TIMEOUT = 1800  # large repos can take time
PROXY_TIMEOUT = 300
MIRROR_WAIT_TIMEOUT = 300  # how long to poll for mirror to become ready

# Allowed characters in GitHub owner/repo names
_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')

logging.basicConfig(
  level=logging.INFO,
  format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("git-proxy")

if not FORGEJO_TOKEN:
  log.warning("FORGEJO_TOKEN is not set — API calls will fail with 401")

PROXY_CONFIG_PATH = os.environ.get("PROXY_CONFIG", "/config/hooks.yml")
_proxy_config = None


def forgejo_api(method: str, path: str, body: dict | None = None, timeout: int = FORGEJO_API_TIMEOUT) -> tuple[int, dict]:
  url = f"{FORGEJO_URL}/api/v1{path}"
  data = json.dumps(body).encode() if body is not None else None
  req = urllib.request.Request(
    url,
    data=data,
    method=method,
    headers={
      "Authorization": f"token {FORGEJO_TOKEN}",
      "Content-Type": "application/json",
    },
  )
  try:
    with urllib.request.urlopen(req, timeout=timeout) as resp:
      raw = resp.read()
      return resp.status, (json.loads(raw) if raw else {})
  except urllib.error.HTTPError as e:
    try:
      return e.code, json.loads(e.read())
    except Exception:
      return e.code, {}


# Mirror repo name in Forgejo encodes both owner and repo to avoid collisions.
def mirror_name(owner: str, repo: str) -> str:
  return f"{owner}__{repo}"


def get_mirror(owner: str, repo: str) -> dict | None:
  name = mirror_name(owner, repo)
  status, data = forgejo_api("GET", f"/repos/{FORGEJO_USER}/{name}")
  if status == 200:
    return data
  return None


def _do_migrate(owner: str, repo: str):
  clone_addr = f"{GITHUB_BASE}/{owner}/{repo}.git"
  name = mirror_name(owner, repo)
  status, data = forgejo_api("POST", "/repos/migrate", {
    "clone_addr": clone_addr,
    "repo_name": name,
    "description": f"Mirror of github.com/{owner}/{repo}",
    "mirror": True,
    "private": False,
    "uid": 1,
  }, timeout=FORGEJO_MIGRATE_TIMEOUT)
  if status not in (201, 409):
    log.warning(f"migrate {owner}/{repo} failed: {status} {data}")
    return
  log.info(f"migrate {owner}/{repo} done: {status}")
  # Register webhook after mirror is created, if a hook config exists for this repo
  if _proxy_config is not None:
    source_repo = f"{owner}/{repo}"
    hook = next((h for h in _proxy_config.hooks if h.source_repo == source_repo), None)
    if hook is not None:
      try:
        _hooks._register_webhook_for_hook(_proxy_config, hook)
      except Exception as e:
        log.error(f"failed to register webhook after migrate for {source_repo}: {e}")


def create_mirror(owner: str, repo: str):
  t = threading.Thread(target=_do_migrate, args=(owner, repo), daemon=True)
  t.start()


_sync_triggered: set[str] = set()
_sync_triggered_lock = threading.Lock()


def _do_mirror_sync(owner: str, repo: str):
  name = mirror_name(owner, repo)
  status, data = forgejo_api("POST", f"/repos/{FORGEJO_USER}/{name}/mirror-sync")
  if status == 200:
    log.info(f"mirror-sync triggered: {owner}/{repo}")
  else:
    log.warning(f"mirror-sync failed: {owner}/{repo}: {status} {data}")
  with _sync_triggered_lock:
    _sync_triggered.discard(f"{owner}/{repo}")


def trigger_mirror_sync(owner: str, repo: str):
  key = f"{owner}/{repo}"
  with _sync_triggered_lock:
    if key in _sync_triggered:
      log.info(f"mirror-sync already in progress: {owner}/{repo}")
      return
    _sync_triggered.add(key)
  t = threading.Thread(target=_do_mirror_sync, args=(owner, repo), daemon=True)
  t.start()


def pkt_line(s: str) -> bytes:
  data = s.encode()
  length = len(data) + 4
  return f"{length:04x}".encode() + data


def git_error_body(message: str) -> bytes:
  return (
    pkt_line("# service=git-upload-pack\n")
    + b"0000"
    + pkt_line(f"ERR {message}")
  )


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
  daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  def log_message(self, format, *args):
    log.info(f"{self.client_address[0]} {format % args}")

  def wait_for_mirror(self, owner: str, repo: str) -> dict | None:
    deadline = time.monotonic() + MIRROR_WAIT_TIMEOUT
    interval = 2
    while time.monotonic() < deadline:
      time.sleep(interval)
      mirror = get_mirror(owner, repo)
      if mirror is not None and not mirror.get("empty", True):
        log.info(f"mirror ready: {owner}/{repo}")
        return mirror
    return get_mirror(owner, repo)

  def send_git_error(self, message: str):
    log.info(f"git error → client: {message}")
    # ERR in git info/refs format works for both GET and POST in practice,
    # but only GET /info/refs is where the client sees the error before sending data.
    # For POST git-upload-pack we return 403 with a plain message instead.
    if self.command == "POST":
      body = message.encode()
      self.send_response(403)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
    else:
      body = git_error_body(message)
      self.send_response(200)
      self.send_header("Content-Type", "application/x-git-upload-pack-advertisement")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)

  def proxy_to_forgejo(self, owner: str, repo: str):
    name = mirror_name(owner, repo)
    # Strict prefix replacement to avoid corrupting the rest of the path
    prefix = f"/{owner}/{repo}"
    if not self.path.startswith(prefix):
      self.send_response(400)
      self.end_headers()
      return
    remainder = self.path[len(prefix):]
    forgejo_path = f"/{FORGEJO_USER}/{name}{remainder}"
    forgejo_url = f"{FORGEJO_URL}{forgejo_path}"

    log.info(f"proxying to {forgejo_url}")

    basic = base64.b64encode(f"{FORGEJO_USER}:{FORGEJO_PASSWORD}".encode()).decode()
    forward_headers = {
      "Authorization": f"Basic {basic}",
    }
    for key in ("Content-Type", "Content-Length", "Git-Protocol",
                "Content-Encoding", "Accept", "Accept-Encoding", "User-Agent"):
      val = self.headers.get(key)
      if val:
        forward_headers[key] = val

    content_length_str = self.headers.get("Content-Length")
    transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
    if content_length_str is not None:
      body = self.rfile.read(int(content_length_str))
    elif "chunked" in transfer_encoding:
      # Read chunked request body and reassemble
      chunks = []
      while True:
        size_line = self.rfile.readline().strip()
        chunk_size = int(size_line, 16)
        if chunk_size == 0:
          self.rfile.read(2)  # trailing CRLF
          break
        chunks.append(self.rfile.read(chunk_size))
        self.rfile.read(2)  # CRLF after chunk
      body = b"".join(chunks)
      # Send reassembled body with Content-Length
      forward_headers["Content-Length"] = str(len(body))
      forward_headers.pop("Transfer-Encoding", None)
    else:
      body = None

    req = urllib.request.Request(
      forgejo_url,
      data=body,
      method=self.command,
      headers=forward_headers,
    )
    hop_by_hop = {
      "transfer-encoding", "connection", "keep-alive",
      "proxy-authenticate", "proxy-authorization", "te", "trailers", "upgrade",
    }
    try:
      with urllib.request.urlopen(req, timeout=PROXY_TIMEOUT) as resp:
        self.send_response(resp.status)
        has_content_length = False
        for key, val in resp.headers.items():
          if key.lower() not in hop_by_hop:
            self.send_header(key, val)
          if key.lower() == "content-length":
            has_content_length = True
        if not has_content_length:
          self.send_header("Connection", "close")
        self.end_headers()
        while True:
          chunk = resp.read(65536)
          if not chunk:
            break
          self.wfile.write(chunk)
        self.wfile.flush()
    except urllib.error.HTTPError as e:
      self.send_response(e.code)
      self.send_header("Content-Length", "0")
      self.end_headers()
    except BrokenPipeError:
      pass

  def handle_git_request(self):
    # Parse and validate owner/repo from path
    # Expected: /owner/repo/...  or  /owner/repo.git/...
    path_only = self.path.split("?")[0]
    parts = path_only.lstrip("/").split("/")
    if len(parts) < 2:
      self.send_response(400)
      self.end_headers()
      return

    owner = parts[0]
    repo = parts[1].removesuffix(".git")

    if not _NAME_RE.match(owner) or not _NAME_RE.match(repo):
      log.warning(f"rejected invalid owner/repo: {owner!r}/{repo!r}")
      self.send_response(400)
      self.end_headers()
      return

    log.info(f"request for github.com/{owner}/{repo}")

    mirror = get_mirror(owner, repo)

    if mirror is None:
      log.info(f"mirror not found, creating github.com/{owner}/{repo}")
      create_mirror(owner, repo)
    elif mirror.get("empty", True):
      log.info(f"mirror exists but empty, triggering sync: {owner}/{repo}")
      trigger_mirror_sync(owner, repo)

    if mirror is None or mirror.get("empty", True):
      log.info(f"waiting for mirror to sync: {owner}/{repo}")
      mirror = self.wait_for_mirror(owner, repo)

    if mirror is None or mirror.get("empty", True):
      self.send_git_error("mirror is syncing, retry in a moment")
      return

    self.proxy_to_forgejo(owner, repo)

  def do_GET(self):
    self.handle_git_request()

  def do_POST(self):
    if self.path.split("?")[0] == _hooks.WEBHOOK_PATH:
      self._handle_webhook()
      return
    self.handle_git_request()

  def _handle_webhook(self):
    if _proxy_config is None:
      body = b"webhook functionality not configured"
      self.send_response(503)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
      return

    length = int(self.headers.get("Content-Length", 0))
    body = self.rfile.read(length)

    status, message = _hooks.handle_forgejo_webhook(body, self.headers, _proxy_config)

    response = message.encode()
    self.send_response(status)
    self.send_header("Content-Type", "text/plain")
    self.send_header("Content-Length", str(len(response)))
    self.end_headers()
    self.wfile.write(response)


if __name__ == "__main__":
  _proxy_config = load_config(PROXY_CONFIG_PATH)
  if _proxy_config:
    log.info(f"Loaded {len(_proxy_config.hooks)} hook(s) from {PROXY_CONFIG_PATH}")
    _hooks.init(forgejo_api, FORGEJO_USER, LISTEN_PORT)
    _hooks.register_all_webhooks_async(_proxy_config)
  else:
    log.info("No hook config loaded — webhook functionality disabled")

  log.info(f"git-proxy listening on port {LISTEN_PORT}")
  log.info(f"Forgejo: {FORGEJO_URL}, user: {FORGEJO_USER}")
  server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
  server.serve_forever()
