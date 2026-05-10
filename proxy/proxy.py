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
import shlex
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import urllib.error
import json
from collections import OrderedDict
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
LS_REMOTE_TIMEOUT = 20  # seconds for `git ls-remote` to upstream (DNS+TLS+ref enumeration on large repos)
LS_REMOTE_CACHE_TTL = 30  # seconds to cache upstream refs
LS_REMOTE_CACHE_MAX = 1000  # cap on number of cached repos to prevent unbounded growth
REF_SYNC_WAIT_TIMEOUT = 120  # how long to wait after triggering sync for refs to converge

# Allowed characters in GitHub owner/repo names
_NAME_RE = re.compile(r'^[A-Za-z0-9_.-]{1,100}$')

class _StructuredFormatter(logging.Formatter):
  """Appends `extra=` kwargs as ` key=value` pairs after the message for easy grep/parsing."""
  _RESERVED = frozenset({
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message", "asctime", "taskName",
  })

  def format(self, record: logging.LogRecord) -> str:
    base = super().format(record)
    extras = []
    for key, value in record.__dict__.items():
      if key in self._RESERVED or key.startswith("_"):
        continue
      # Quote values that contain whitespace, quotes, or other shell-special chars
      # so the line remains parseable (key=value pairs separated by single spaces).
      extras.append(f"{key}={shlex.quote(str(value))}")
    if extras:
      return f"{base} {' '.join(extras)}"
    return base


_handler = logging.StreamHandler()
_handler.setFormatter(_StructuredFormatter("%(asctime)s %(levelname)s %(message)s"))
log = logging.getLogger("git-proxy")
log.setLevel(logging.INFO)
log.addHandler(_handler)
log.propagate = False

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
  key = f"{owner}/{repo}"
  if status not in (201, 409):
    log.warning("migrate failed", extra={"repo": key, "status": status, "body": str(data)[:200]})
    return
  log.info("migrate done", extra={"repo": key, "status": status})
  # Register webhook after mirror is created, if a hook config exists for this repo
  if _proxy_config is not None:
    hook = next((h for h in _proxy_config.hooks if h.source_repo == key), None)
    if hook is not None:
      try:
        _hooks._register_webhook_for_hook(_proxy_config, hook)
      except Exception as e:
        log.error("webhook registration failed after migrate", extra={"repo": key, "error": str(e)})


def create_mirror(owner: str, repo: str):
  t = threading.Thread(target=_do_migrate, args=(owner, repo), daemon=True)
  t.start()


def _parse_ls_remote(output: str) -> dict[str, str]:
  refs = {}
  for line in output.splitlines():
    parts = line.split("\t", 1)
    if len(parts) != 2:
      continue
    name = parts[1]
    # Peeled tag entries (`refs/tags/v1^{}`) are emitted on one side but possibly
    # not the other depending on git version; comparing them would create spurious diffs.
    if name.endswith("^{}"):
      continue
    if name.startswith("refs/heads/") or name.startswith("refs/tags/"):
      refs[name] = parts[0]
  return refs


class MirrorFreshness:
  """Encapsulates per-repo freshness state: upstream ls-remote cache, in-flight ls-remote
  events, in-flight mirror-sync flags. One global instance is created in __main__ and
  injected into the ProxyHandler via the server reference."""

  def __init__(self, forgejo_url: str, forgejo_user: str, forgejo_password: str,
               cache_ttl: int = LS_REMOTE_CACHE_TTL, cache_max: int = LS_REMOTE_CACHE_MAX,
               ls_remote_timeout: int = LS_REMOTE_TIMEOUT, sync_wait_timeout: int = REF_SYNC_WAIT_TIMEOUT):
    self._forgejo_url = forgejo_url
    self._forgejo_user = forgejo_user
    self._forgejo_password = forgejo_password
    self._cache_ttl = cache_ttl
    self._cache_max = cache_max
    self._ls_remote_timeout = ls_remote_timeout
    self._sync_wait_timeout = sync_wait_timeout
    self._upstream_cache: "OrderedDict[str, tuple[float, dict[str, str]]]" = OrderedDict()
    self._cache_lock = threading.Lock()
    self._inflight_ls_remote: dict[str, threading.Event] = {}
    self._sync_triggered: set[str] = set()
    self._sync_lock = threading.Lock()

  def upstream_refs(self, owner: str, repo: str) -> dict[str, str] | None:
    """Returns {refname: sha} for refs/heads/* and refs/tags/* on GitHub, or None on error.
    Single-flight: concurrent calls for the same repo coalesce into one ls-remote.
    Edge case: empty upstream repo returns empty dict (not None)."""
    key = f"{owner}/{repo}"
    with self._cache_lock:
      cached = self._upstream_cache.get(key)
      if cached and time.monotonic() - cached[0] < self._cache_ttl:
        self._upstream_cache.move_to_end(key)
        return cached[1]
      inflight = self._inflight_ls_remote.get(key)
      is_leader = inflight is None
      if is_leader:
        inflight = threading.Event()
        self._inflight_ls_remote[key] = inflight

    if not is_leader:
      inflight.wait(timeout=self._ls_remote_timeout + 5)
      with self._cache_lock:
        cached = self._upstream_cache.get(key)
        # Apply same TTL check as the fast path: if the only available entry is older
        # than cache_ttl (e.g. leader's fetch failed and left a stale entry), report None
        # so callers see the failure rather than silently serving stale data.
        if cached and time.monotonic() - cached[0] < self._cache_ttl:
          return cached[1]
        return None

    try:
      return self._fetch_upstream_refs(owner, repo, key)
    finally:
      with self._cache_lock:
        self._inflight_ls_remote.pop(key, None)
      inflight.set()

  def _fetch_upstream_refs(self, owner: str, repo: str, key: str) -> dict[str, str] | None:
    url = f"{GITHUB_BASE}/{owner}/{repo}.git"
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    start = time.monotonic()
    try:
      result = subprocess.run(
        ["git", "ls-remote", url, "refs/heads/*", "refs/tags/*"],
        capture_output=True, text=True, timeout=self._ls_remote_timeout, env=env,
      )
    except subprocess.TimeoutExpired:
      log.error("upstream ls-remote timeout", extra={"repo": key, "timeout_s": self._ls_remote_timeout, "result": "timeout"})
      return None
    except Exception as e:
      log.error("upstream ls-remote exception", extra={
        "repo": key, "elapsed_ms": int((time.monotonic() - start) * 1000),
        "error": str(e), "result": "exception",
      })
      return None

    elapsed_ms = int((time.monotonic() - start) * 1000)
    if result.returncode != 0:
      log.error("upstream ls-remote non-zero rc", extra={
        "repo": key, "rc": result.returncode, "elapsed_ms": elapsed_ms,
        "stderr": result.stderr.strip()[:200], "result": "error",
      })
      return None

    refs = _parse_ls_remote(result.stdout)
    log.info("upstream ls-remote ok", extra={"repo": key, "elapsed_ms": elapsed_ms, "refs_count": len(refs), "result": "ok"})
    with self._cache_lock:
      self._upstream_cache[key] = (time.monotonic(), refs)
      self._upstream_cache.move_to_end(key)
      while len(self._upstream_cache) > self._cache_max:
        self._upstream_cache.popitem(last=False)
    return refs

  def forgejo_refs(self, owner: str, repo: str) -> dict[str, str] | None:
    """Returns {refname: sha} for refs/heads/* and refs/tags/* in the Forgejo mirror, or None on error.
    Credentials are passed via GIT_CONFIG_* env vars to avoid leaking the basic-auth header in argv."""
    name = mirror_name(owner, repo)
    key = f"{owner}/{repo}"
    url = f"{self._forgejo_url}/{self._forgejo_user}/{name}.git"
    header = f"Authorization: Basic {base64.b64encode(f'{self._forgejo_user}:{self._forgejo_password}'.encode()).decode()}"
    env = {
      **os.environ,
      "GIT_TERMINAL_PROMPT": "0",
      "GIT_CONFIG_COUNT": "1",
      "GIT_CONFIG_KEY_0": "http.extraHeader",
      "GIT_CONFIG_VALUE_0": header,
    }
    start = time.monotonic()
    try:
      result = subprocess.run(
        ["git", "ls-remote", url, "refs/heads/*", "refs/tags/*"],
        capture_output=True, text=True, timeout=self._ls_remote_timeout, env=env,
      )
    except Exception as e:
      log.error("forgejo ls-remote exception", extra={
        "repo": key, "elapsed_ms": int((time.monotonic() - start) * 1000),
        "error": str(e), "result": "exception",
      })
      return None

    if result.returncode != 0:
      log.error("forgejo ls-remote non-zero rc", extra={
        "repo": key, "rc": result.returncode,
        "elapsed_ms": int((time.monotonic() - start) * 1000),
        "stderr": result.stderr.strip()[:200], "result": "error",
      })
      return None

    return _parse_ls_remote(result.stdout)

  def trigger_sync(self, owner: str, repo: str) -> bool:
    """Triggers Forgejo mirror-sync via API. Returns False if a sync is already in flight."""
    key = f"{owner}/{repo}"
    with self._sync_lock:
      if key in self._sync_triggered:
        log.info("mirror-sync dedup: already in flight", extra={"repo": key})
        return False
      self._sync_triggered.add(key)
    t = threading.Thread(target=self._do_sync, args=(owner, repo, key), daemon=True)
    t.start()
    return True

  def _do_sync(self, owner: str, repo: str, key: str):
    start = time.monotonic()
    try:
      name = mirror_name(owner, repo)
      status, data = forgejo_api("POST", f"/repos/{self._forgejo_user}/{name}/mirror-sync")
      elapsed_ms = int((time.monotonic() - start) * 1000)
      if status == 200:
        log.info("mirror-sync triggered", extra={"repo": key, "elapsed_ms": elapsed_ms, "result": "ok"})
      else:
        log.warning("mirror-sync api error", extra={"repo": key, "elapsed_ms": elapsed_ms, "status": status, "body": str(data)[:200], "result": "error"})
    finally:
      with self._sync_lock:
        self._sync_triggered.discard(key)

  def wait_for_ref_sync(self, owner: str, repo: str, expected: dict[str, str],
                        timeout: int | None = None) -> bool:
    """Polls Forgejo refs until every (refname, sha) in `expected` is present in the mirror,
    or timeout expires. Extra refs in the mirror are ignored. Returns True on match, False on timeout."""
    timeout = timeout if timeout is not None else self._sync_wait_timeout
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
      current = self.forgejo_refs(owner, repo)
      if current is not None and all(current.get(r) == s for r, s in expected.items()):
        return True
      time.sleep(1.5)
    return False

  def ensure_fresh(self, owner: str, repo: str) -> bool:
    """Compares Forgejo mirror refs with upstream (heads+tags only). If they differ,
    triggers mirror-sync and waits up to sync_wait_timeout. On any upstream error: logs and
    returns False (fail-open — caller still proxies). Returns True if mirror is/became in sync."""
    key = f"{owner}/{repo}"
    upstream = self.upstream_refs(owner, repo)
    if upstream is None:
      return False
    current = self.forgejo_refs(owner, repo)
    if current is None:
      return False
    if current == upstream:
      return True

    added = sum(1 for r in upstream if r not in current)
    changed = sum(1 for r, s in upstream.items() if r in current and current[r] != s)
    removed = sum(1 for r in current if r not in upstream)
    log.info("mirror stale", extra={"repo": key, "added": added, "changed": changed, "removed": removed})
    self.trigger_sync(owner, repo)
    start = time.monotonic()
    if self.wait_for_ref_sync(owner, repo, upstream):
      log.info("mirror converged", extra={"repo": key, "elapsed_ms": int((time.monotonic() - start) * 1000), "result": "ok"})
      return True
    log.error("mirror did not converge", extra={
      "repo": key, "timeout_s": self._sync_wait_timeout,
      "elapsed_ms": int((time.monotonic() - start) * 1000), "result": "timeout",
    })
    return False


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
    log.info("http access", extra={"client": self.client_address[0], "line": format % args})

  def wait_for_mirror(self, owner: str, repo: str) -> dict | None:
    deadline = time.monotonic() + MIRROR_WAIT_TIMEOUT
    interval = 2
    while time.monotonic() < deadline:
      time.sleep(interval)
      mirror = get_mirror(owner, repo)
      if mirror is not None and not mirror.get("empty", True):
        log.info("mirror ready", extra={"repo": f"{owner}/{repo}"})
        return mirror
    return get_mirror(owner, repo)

  def send_git_error(self, message: str):
    log.info("returning git error to client", extra={"message": message})
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

    log.info("proxying to forgejo", extra={"forgejo_url": forgejo_url})

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
      log.warning("rejected invalid owner/repo", extra={"owner": repr(owner), "repo_name": repr(repo)})
      self.send_response(400)
      self.end_headers()
      return

    log.info("request received", extra={"repo": f"{owner}/{repo}", "method": self.command, "path": self.path})

    mirror = get_mirror(owner, repo)
    freshness: MirrorFreshness = self.server.freshness  # type: ignore[attr-defined]

    if mirror is None:
      log.info("mirror create-on-demand", extra={"repo": f"{owner}/{repo}"})
      create_mirror(owner, repo)
    elif mirror.get("empty", True):
      log.info("mirror empty, triggering sync", extra={"repo": f"{owner}/{repo}"})
      freshness.trigger_sync(owner, repo)

    if mirror is None or mirror.get("empty", True):
      log.info("waiting for mirror to populate", extra={"repo": f"{owner}/{repo}"})
      mirror = self.wait_for_mirror(owner, repo)

    if mirror is None or mirror.get("empty", True):
      self.send_git_error("mirror is syncing, retry in a moment")
      return

    if self.command == "GET" and len(parts) == 4 and parts[2] == "info" and parts[3] == "refs":
      query = self.path.split("?", 1)[1] if "?" in self.path else ""
      service = urllib.parse.parse_qs(query).get("service", [""])[0]
      if service == "git-upload-pack":
        freshness.ensure_fresh(owner, repo)

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
    log.info("hook config loaded", extra={"hook_count": len(_proxy_config.hooks), "path": PROXY_CONFIG_PATH})
    _hooks.init(forgejo_api, FORGEJO_USER, LISTEN_PORT)
    _hooks.register_all_webhooks_async(_proxy_config)
  else:
    log.info("no hook config loaded — webhook functionality disabled")

  log.info("git-proxy starting", extra={"port": LISTEN_PORT, "forgejo_url": FORGEJO_URL, "forgejo_user": FORGEJO_USER})
  server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
  server.freshness = MirrorFreshness(FORGEJO_URL, FORGEJO_USER, FORGEJO_PASSWORD)
  server.serve_forever()
