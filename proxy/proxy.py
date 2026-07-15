#!/usr/bin/env python3
"""
git-proxy: HTTP proxy that mirrors public git repositories into Forgejo on demand.

Any allowlisted upstream host (github.com, gitlab.com, self-hosted GitLab, ...) is
supported; the host is encoded as the first path segment. Client configures one
`insteadOf` per host:
  git config url."http://<proxy-host>:8080/github.com/".insteadOf "https://github.com/"
  git config url."http://<proxy-host>:8080/gitlab.com/".insteadOf "https://gitlab.com/"

Then `git clone https://gitlab.com/group/sub/repo` becomes
     `git clone http://<proxy-host>:8080/gitlab.com/group/sub/repo`

Proxy behaviour:
  - Mirror exists and is synced  → proxy the request to Forgejo
  - Mirror exists but empty      → return git ERR (retry)
  - Mirror does not exist        → create mirror, return git ERR (retry)
  - POST /git-receive-pack       → 403 (mirrors are read-only)
"""

import base64
import logging
import os
import shlex
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
import source
from source import SourceRepo, parse_request_path

FORGEJO_URL = os.environ.get("FORGEJO_URL", "http://127.0.0.1:3000")
FORGEJO_TOKEN = os.environ.get("FORGEJO_TOKEN", "")
FORGEJO_USER = os.environ.get("FORGEJO_USER", "gitadmin")
FORGEJO_PASSWORD = os.environ.get("FORGEJO_PASSWORD", "")
LISTEN_PORT = int(os.environ.get("PROXY_PORT", "8080"))
FORGEJO_API_TIMEOUT = 30
FORGEJO_MIGRATE_TIMEOUT = 1800  # large repos can take time
PROXY_TIMEOUT = 300
MIRROR_WAIT_TIMEOUT = 300  # how long to poll for mirror to become ready
SYNC_FRESHNESS_TTL = int(os.environ.get("PROXY_SYNC_TTL", "30"))  # seconds a mirror is considered fresh after a successful sync
SYNC_WAIT_TIMEOUT = 120  # how long to wait (mode=wait) for mirror-sync to complete, polling mirror_updated
SYNC_CACHE_MAX = 1000  # cap on number of repos tracked in the freshness cache to prevent unbounded growth
REQUEST_BODY_MAX = 512 * 1024 * 1024  # cap on a reassembled request body; upload-pack negotiation is small, pushes are 403'd earlier


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

_SYNC_MODES = frozenset({"wait", "async"})
SYNC_MODE = os.environ.get("PROXY_SYNC_MODE", "wait")  # "wait" (default) or "async"
if SYNC_MODE not in _SYNC_MODES:
  log.warning("invalid PROXY_SYNC_MODE, falling back to 'wait'", extra={"value": SYNC_MODE})
  SYNC_MODE = "wait"

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


def get_mirror(source_repo: SourceRepo) -> dict | None:
  name = source_repo.mirror_name()
  status, data = forgejo_api("GET", f"/repos/{FORGEJO_USER}/{name}")
  if status == 200:
    return data
  return None


def _do_migrate(source_repo: SourceRepo):
  clone_addr = source.REGISTRY.clone_addr(source_repo)
  name = source_repo.mirror_name()
  key = source_repo.key()
  status, data = forgejo_api("POST", "/repos/migrate", {
    "clone_addr": clone_addr,
    "repo_name": name,
    "description": f"Mirror of {source.REGISTRY.source_url(source_repo)}",
    "mirror": True,
    "private": False,
    "uid": 1,
  }, timeout=FORGEJO_MIGRATE_TIMEOUT)
  if status not in (201, 409):
    log.warning("migrate failed", extra={"repo": key, "status": status, "body": str(data)[:200]})
    return
  log.info("migrate done", extra={"repo": key, "status": status})
  # Register webhook after mirror is created, if a hook config exists for this repo
  if _proxy_config is not None:
    hook = next((h for h in _proxy_config.hooks if h.source_repo().key() == key), None)
    if hook is not None:
      try:
        _hooks._register_webhook_for_hook(_proxy_config, hook)
      except Exception as e:
        log.error("webhook registration failed after migrate", extra={"repo": key, "error": str(e)})


def create_mirror(source_repo: SourceRepo):
  t = threading.Thread(target=_do_migrate, args=(source_repo,), daemon=True)
  t.start()


class MirrorFreshness:
  """Encapsulates per-repo freshness state: a freshness cache (recent-sync timestamps) and
  in-flight mirror-sync tracking for single-flight dedup. One global instance is created in
  __main__ and injected into the ProxyHandler via the server reference.

  Freshness model: sync-on-demand. Instead of comparing refs between GitHub and the mirror,
  we simply ask Forgejo to mirror-sync and (in "wait" mode) wait for it to actually finish,
  or (in "async" mode) trigger it and return immediately, serving whatever the mirror
  currently has. A short freshness cache (freshness_ttl) avoids syncing on every request."""

  def __init__(self, forgejo_url: str, forgejo_user: str, forgejo_password: str,
               sync_mode: str = "wait", freshness_ttl: int = 30, sync_wait_timeout: int = 120,
               cache_max: int = SYNC_CACHE_MAX):
    self._forgejo_url = forgejo_url
    self._forgejo_user = forgejo_user
    self._forgejo_password = forgejo_password
    self._sync_mode = sync_mode
    self._freshness_ttl = freshness_ttl
    self._sync_wait_timeout = sync_wait_timeout
    self._cache_max = cache_max
    self._last_sync: "OrderedDict[str, float]" = OrderedDict()
    self._freshness_lock = threading.Lock()
    # Single-flight for trigger_sync() (fire-and-forget dedup, used for empty mirrors).
    self._sync_triggered: set[str] = set()
    self._sync_lock = threading.Lock()
    # Single-flight for ensure_synced() (sync-on-demand dedup, independent of the above:
    # ensure_synced needs to know when the leader's sync *finished*, not just that one
    # was fired off, so followers in "wait" mode can join it).
    self._ensure_inflight: dict[str, threading.Event] = {}
    self._ensure_lock = threading.Lock()

  def _is_fresh(self, key: str) -> bool:
    with self._freshness_lock:
      last = self._last_sync.get(key)
      return last is not None and time.monotonic() - last < self._freshness_ttl

  def _mark_fresh(self, key: str):
    with self._freshness_lock:
      self._last_sync[key] = time.monotonic()
      self._last_sync.move_to_end(key)
      while len(self._last_sync) > self._cache_max:
        self._last_sync.popitem(last=False)

  def _get_mirror_updated(self, source_repo: SourceRepo, timeout: float = FORGEJO_API_TIMEOUT) -> tuple[bool, str | None]:
    """Returns (ok, value): ok=False means the API call itself failed (exception / non-200) —
    the caller cannot trust `value` at all in that case. ok=True means the request succeeded;
    `value` is the `mirror_updated` field, which may legitimately be None/empty (mirror never
    synced yet) — that is NOT an error and must not be conflated with an API failure.
    `timeout` bounds the underlying HTTP call so the poll loop can honor its own deadline."""
    name = source_repo.mirror_name()
    try:
      status, data = forgejo_api("GET", f"/repos/{self._forgejo_user}/{name}", timeout=timeout)
    except Exception as e:
      log.error("get mirror_updated exception", extra={"repo": source_repo.key(), "error": str(e)})
      return False, None
    if status != 200:
      log.error("get mirror_updated failed", extra={"repo": source_repo.key(), "status": status})
      return False, None
    return True, data.get("mirror_updated")

  def ensure_synced(self, source_repo: SourceRepo) -> bool:
    """Ensures the Forgejo mirror is synced with GitHub, sync-on-demand with a freshness cache.

    - If the mirror was synced successfully within freshness_ttl seconds, returns True immediately.
    - Otherwise triggers (or joins an in-flight) mirror-sync:
        mode "wait":  waits (polling mirror_updated) up to sync_wait_timeout for the sync to
                      actually complete, then returns True/False.
        mode "async": triggers the sync in the background and returns True immediately without
                      waiting — the caller serves whatever the mirror currently has.
    Fail-open: never raises; any API/network error or timeout is logged and returns False,
    the caller proxies regardless."""
    key = source_repo.key()

    if self._is_fresh(key):
      return True

    with self._ensure_lock:
      inflight = self._ensure_inflight.get(key)
      is_leader = inflight is None
      if is_leader:
        inflight = threading.Event()
        self._ensure_inflight[key] = inflight

    if not is_leader:
      if self._sync_mode == "async":
        # Don't wait for someone else's sync in async mode — serve current mirror state.
        return True
      if not inflight.wait(timeout=self._sync_wait_timeout + 5):
        log.error("ensure_synced: timed out waiting for in-flight sync", extra={"repo": key})
        return False
      return self._is_fresh(key)

    if self._sync_mode == "async":
      # Leader kicks off a background thread that does the real POST + wait-for-completion
      # + _mark_fresh, and only THAT thread releases _ensure_inflight[key] when it's done.
      # ensure_synced itself returns immediately without waiting, so the caller serves
      # whatever the mirror currently has — but the mirror is only ever marked fresh once
      # the background sync has genuinely completed, never optimistically.
      t = threading.Thread(target=self._run_sync_async_bg, args=(source_repo, key, inflight), daemon=True)
      try:
        t.start()
      except Exception as e:
        # If the thread can't start (e.g. RuntimeError under thread/fd exhaustion), the
        # background body's finally never runs, so we MUST release the in-flight slot here —
        # otherwise the key stays stuck forever and this repo silently stops being synced.
        log.error("ensure_synced: failed to start async sync thread", extra={"repo": key, "error": str(e)})
        with self._ensure_lock:
          self._ensure_inflight.pop(key, None)
        inflight.set()
        return False
      return True

    try:
      try:
        return self._run_sync_wait(source_repo, key)
      except Exception as e:
        # Belt-and-braces: _run_sync_wait already guards its own forgejo_api calls, but this
        # outer catch makes the "never raises" contract structural rather than incidental
        # to which lines happen to have a try/except today (e.g. _mark_fresh, time.sleep).
        log.error("ensure_synced: unexpected exception in leader sync", extra={"repo": key, "error": str(e)})
        return False
    finally:
      with self._ensure_lock:
        self._ensure_inflight.pop(key, None)
      inflight.set()

  def _run_sync_wait(self, source_repo: SourceRepo, key: str) -> bool:
    """Leader path for sync_mode="wait": POST mirror-sync, then synchronously wait for it
    to actually complete (or time out), returning True/False accordingly. Wrapped by
    ensure_synced in a blanket try/except so an unexpected exception anywhere in here (not
    just around the forgejo_api calls) still honors the fail-open contract."""
    ok, prev_updated = self._get_mirror_updated(source_repo)
    if not ok:
      # Couldn't even get a pre-sync snapshot — fail open without a blind poll loop,
      # which would otherwise have no baseline to detect convergence against.
      log.error("mirror-sync aborted: could not fetch pre-sync mirror_updated", extra={"repo": key})
      return False

    if not self._trigger_mirror_sync(source_repo, key):
      return False

    if self._wait_for_sync_completion(source_repo, key, prev_updated):
      self._mark_fresh(key)
      return True
    return False

  def _run_sync_async_bg(self, source_repo: SourceRepo, key: str, inflight: threading.Event):
    """Background thread body for sync_mode="async": POST mirror-sync, wait for it to
    actually complete (same criterion as "wait" mode), and only mark the mirror fresh if it
    genuinely converged — never optimistically. Always releases _ensure_inflight[key] and
    the inflight Event in `finally`, even on an unexpected exception, so the key never gets
    stuck and a later call (after this one finishes, successfully or not) can start fresh."""
    try:
      ok, prev_updated = self._get_mirror_updated(source_repo)
      if not ok:
        log.error("mirror-sync (async bg) aborted: could not fetch pre-sync mirror_updated", extra={"repo": key})
        return
      if not self._trigger_mirror_sync(source_repo, key):
        return
      if self._wait_for_sync_completion(source_repo, key, prev_updated):
        self._mark_fresh(key)
    except Exception as e:
      log.error("mirror-sync (async bg) unexpected exception", extra={"repo": key, "error": str(e)})
    finally:
      with self._ensure_lock:
        self._ensure_inflight.pop(key, None)
      inflight.set()

  def _trigger_mirror_sync(self, source_repo: SourceRepo, key: str) -> bool:
    """POSTs /mirror-sync. Returns True on 200, False (logged) on any error/exception."""
    name = source_repo.mirror_name()
    try:
      status, data = forgejo_api("POST", f"/repos/{self._forgejo_user}/{name}/mirror-sync")
    except Exception as e:
      log.error("mirror-sync api exception", extra={"repo": key, "error": str(e)})
      return False
    if status != 200:
      log.error("mirror-sync api error", extra={"repo": key, "status": status, "body": str(data)[:200]})
      return False
    log.info("mirror-sync triggered", extra={"repo": key, "sync_mode": self._sync_mode})
    return True

  def _wait_for_sync_completion(self, source_repo: SourceRepo, key: str, prev_updated: str | None) -> bool:
    """Polls mirror_updated until it transitions to a genuinely new, non-empty value (relative
    to prev_updated), or sync_wait_timeout elapses. Shared by the "wait" leader path and the
    "async" background thread — the completion criterion is identical either way; only who
    waits for it (the client vs. a background thread) differs. Does NOT call _mark_fresh —
    that's the caller's responsibility on True. A prev_updated of None/"" means the mirror
    never synced before; in that case any non-None/non-empty current value that differs from
    prev_updated counts (covers the common "freshly created mirror" case without falsely
    declaring victory on the very first poll)."""
    start = time.monotonic()
    deadline = start + self._sync_wait_timeout
    while time.monotonic() < deadline:
      time.sleep(1.5)
      # Bound the poll's HTTP call by the time left until the deadline, so a single slow/hung
      # request can't overrun sync_wait_timeout by up to a full FORGEJO_API_TIMEOUT.
      remaining = deadline - time.monotonic()
      if remaining <= 0:
        break
      ok, current_updated = self._get_mirror_updated(source_repo, timeout=min(FORGEJO_API_TIMEOUT, remaining))
      if not ok:
        # Transient API error while polling — don't treat it as convergence, just keep
        # polling until the timeout.
        continue
      if current_updated and current_updated != prev_updated:
        log.info("mirror-sync converged", extra={
          "repo": key, "elapsed_ms": int((time.monotonic() - start) * 1000), "result": "ok",
        })
        return True

    log.error("mirror-sync did not converge", extra={
      "repo": key, "timeout_s": self._sync_wait_timeout,
      "elapsed_ms": int((time.monotonic() - start) * 1000), "result": "timeout",
    })
    return False

  def trigger_sync(self, source_repo: SourceRepo) -> bool:
    """Triggers Forgejo mirror-sync via API. Returns False if a sync is already in flight."""
    key = source_repo.key()
    with self._sync_lock:
      if key in self._sync_triggered:
        log.info("mirror-sync dedup: already in flight", extra={"repo": key})
        return False
      self._sync_triggered.add(key)
    t = threading.Thread(target=self._do_sync, args=(source_repo, key), daemon=True)
    t.start()
    return True

  def _do_sync(self, source_repo: SourceRepo, key: str):
    start = time.monotonic()
    try:
      name = source_repo.mirror_name()
      status, data = forgejo_api("POST", f"/repos/{self._forgejo_user}/{name}/mirror-sync")
      elapsed_ms = int((time.monotonic() - start) * 1000)
      if status == 200:
        log.info("mirror-sync triggered", extra={"repo": key, "elapsed_ms": elapsed_ms, "result": "ok"})
      else:
        log.warning("mirror-sync api error", extra={"repo": key, "elapsed_ms": elapsed_ms, "status": status, "body": str(data)[:200], "result": "error"})
    finally:
      with self._sync_lock:
        self._sync_triggered.discard(key)


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


def _is_upload_pack_request(service: str, command: str, query: str) -> bool:
  """True for the two request shapes that need mirror freshness: the ref advertisement
  (GET .../info/refs?service=git-upload-pack) and the actual fetch (POST .../git-upload-pack,
  which carries `want <sha>` lines — including arbitrary commit SHAs, not just branch tips)."""
  if command == "GET" and service == "/info/refs":
    svc = urllib.parse.parse_qs(query).get("service", [""])[0]
    return svc == "git-upload-pack"
  if command == "POST" and service == "/git-upload-pack":
    return True
  return False


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
  daemon_threads = True


class ProxyHandler(BaseHTTPRequestHandler):
  protocol_version = "HTTP/1.1"
  def log_message(self, format, *args):
    log.info("http access", extra={"client": self.client_address[0], "line": format % args})

  def wait_for_mirror(self, source_repo: SourceRepo) -> dict | None:
    deadline = time.monotonic() + MIRROR_WAIT_TIMEOUT
    interval = 2
    while time.monotonic() < deadline:
      time.sleep(interval)
      mirror = get_mirror(source_repo)
      if mirror is not None and not mirror.get("empty", True):
        log.info("mirror ready", extra={"repo": source_repo.key()})
        return mirror
    return get_mirror(source_repo)

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

  def _read_chunked_body(self, max_bytes: int) -> bytes:
    """Reassemble a Transfer-Encoding: chunked request body. Raises ValueError on a
    malformed/oversized stream (invalid chunk size, EOF mid-body, total over max_bytes) so
    the caller can return 400 rather than the handler thread dying on an int() parse."""
    chunks = []
    total = 0
    while True:
      size_line = self.rfile.readline()
      if not size_line:
        raise ValueError("unexpected EOF in chunked body")
      # A chunk-size line may carry ';'-separated chunk extensions — ignore them.
      size_token = size_line.split(b";", 1)[0].strip()
      try:
        chunk_size = int(size_token, 16)
      except ValueError:
        raise ValueError(f"invalid chunk size: {size_token!r}")
      if chunk_size < 0:
        raise ValueError(f"negative chunk size: {chunk_size}")
      if chunk_size == 0:
        self.rfile.read(2)  # trailing CRLF
        break
      total += chunk_size
      if total > max_bytes:
        raise ValueError(f"chunked body exceeds {max_bytes} bytes")
      chunks.append(self.rfile.read(chunk_size))
      self.rfile.read(2)  # CRLF after chunk
    return b"".join(chunks)

  def proxy_to_forgejo(self, source_repo: SourceRepo, service: str):
    name = source_repo.mirror_name()
    # Reconstruct the Forgejo path from the parsed git service + original query string,
    # rather than slicing a prefix off self.path (which would not survive a trailing
    # ".git" on the source or a nested repo path).
    query = self.path.split("?", 1)[1] if "?" in self.path else ""
    remainder = service + ("?" + query if query else "")
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
    try:
      if content_length_str is not None:
        length = int(content_length_str)
        if length < 0 or length > REQUEST_BODY_MAX:
          raise ValueError(f"Content-Length out of range: {length}")
        body = self.rfile.read(length)
      elif "chunked" in transfer_encoding:
        body = self._read_chunked_body(REQUEST_BODY_MAX)
        # Send reassembled body with Content-Length
        forward_headers["Content-Length"] = str(len(body))
        forward_headers.pop("Transfer-Encoding", None)
      else:
        body = None
    except ValueError as e:
      # Malformed framing from an untrusted client — 400 instead of letting the parse
      # exception kill the handler thread.
      log.warning("malformed request body", extra={"repo": source_repo.key(), "error": str(e)})
      self.send_response(400)
      self.send_header("Content-Length", "0")
      self.end_headers()
      return

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
    # Parse and validate /<host>/<repo-path...>/<git-service> against the source allowlist.
    path_only = self.path.split("?")[0]
    parsed = parse_request_path(path_only, source.REGISTRY)
    if parsed is None:
      log.warning("rejected invalid request path", extra={"path": self.path})
      self.send_response(400)
      self.end_headers()
      return

    source_repo, service = parsed
    key = source_repo.key()

    # Mirrors are read-only: reject pushes rather than proxying them upstream.
    if self.command == "POST" and service == "/git-receive-pack":
      log.warning("rejected push to read-only mirror", extra={"repo": key})
      body = b"git-proxy mirrors are read-only"
      self.send_response(403)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", str(len(body)))
      self.end_headers()
      self.wfile.write(body)
      return

    log.info("request received", extra={"repo": key, "method": self.command, "path": self.path})

    mirror = get_mirror(source_repo)
    freshness: MirrorFreshness = self.server.freshness  # type: ignore[attr-defined]

    if mirror is None:
      log.info("mirror create-on-demand", extra={"repo": key})
      create_mirror(source_repo)
    elif mirror.get("empty", True):
      log.info("mirror empty, triggering sync", extra={"repo": key})
      freshness.trigger_sync(source_repo)

    if mirror is None or mirror.get("empty", True):
      log.info("waiting for mirror to populate", extra={"repo": key})
      mirror = self.wait_for_mirror(source_repo)

    if mirror is None or mirror.get("empty", True):
      self.send_git_error("mirror is syncing, retry in a moment")
      return

    query = self.path.split("?", 1)[1] if "?" in self.path else ""
    if _is_upload_pack_request(service, self.command, query):
      synced = freshness.ensure_synced(source_repo)
      log.info("ensure_synced result", extra={"repo": key, "synced": synced})

    self.proxy_to_forgejo(source_repo, service)

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

  log.info("git-proxy starting", extra={
    "port": LISTEN_PORT, "forgejo_url": FORGEJO_URL, "forgejo_user": FORGEJO_USER, "sync_mode": SYNC_MODE,
    "sources": ",".join(source.REGISTRY.hosts()),
  })
  server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), ProxyHandler)
  server.freshness = MirrorFreshness(
    FORGEJO_URL, FORGEJO_USER, FORGEJO_PASSWORD,
    sync_mode=SYNC_MODE, freshness_ttl=SYNC_FRESHNESS_TTL, sync_wait_timeout=SYNC_WAIT_TIMEOUT,
  )
  server.serve_forever()
