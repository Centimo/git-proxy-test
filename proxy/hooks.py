#!/usr/bin/env python3
"""
Webhook handling for git-proxy.

Receives Forgejo push events for mirrored repos, detects release tags,
and runs a user-configured command per hook (see config.HookConfig).
git-proxy itself is domain-agnostic: what to do on a release tag lives
entirely in that external command.
"""

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading

from config import HookConfig, ProxyConfig

log = logging.getLogger("git-proxy")

WEBHOOK_PATH = "/webhook/forgejo"
# git object name: 40 hex chars (sha1) or 64 hex chars (sha256)
_SHA_RE = re.compile(r'^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$')
# URL Forgejo will call back — works because container uses host networking
_PROXY_WEBHOOK_URL = "http://127.0.0.1:{port}/webhook/forgejo"

# Set by proxy.py at startup via init()
_forgejo_api = None
_forgejo_user = None
_listen_port = None


def init(forgejo_api_fn, forgejo_user: str, listen_port: int):
  global _forgejo_api, _forgejo_user, _listen_port
  _forgejo_api = forgejo_api_fn
  _forgejo_user = forgejo_user
  _listen_port = listen_port


def _require_init():
  if _forgejo_api is None:
    raise RuntimeError("hooks.init() has not been called — forgejo API not available")


# ---------------------------------------------------------------------------
# Mirror name helpers (mirrors proxy.py logic, kept independent)
# ---------------------------------------------------------------------------

def _mirror_name(owner: str, repo: str) -> str:
  return f"{owner}__{repo}"


def _parse_mirror_full_name(full_name: str) -> tuple[str, str] | None:
  """
  Reverse of mirror_name: "gitadmin/Centimo__simd" → ("Centimo", "simd").
  Returns None if the name doesn't match the expected pattern.

  Note: ambiguous if owner or repo themselves contain "__" — this is a
  known limitation of the "__" delimiter scheme used by proxy.py.
  """
  slash_pos = full_name.find('/')
  if slash_pos < 0:
    return None
  mirror_part = full_name[slash_pos + 1:]
  dunder_pos = mirror_part.find('__')
  if dunder_pos < 0:
    return None
  owner = mirror_part[:dunder_pos]
  repo = mirror_part[dunder_pos + 2:]
  if not owner or not repo:
    return None
  return owner, repo


# ---------------------------------------------------------------------------
# Forgejo webhook registration
# ---------------------------------------------------------------------------

def _register_webhook_for_hook(cfg: ProxyConfig, hook: HookConfig):
  _require_init()
  owner = hook.owner()
  repo = hook.repo()
  mirror = _mirror_name(owner, repo)
  webhook_url = _PROXY_WEBHOOK_URL.format(port=_listen_port)

  # Check if mirror exists
  status, data = _forgejo_api("GET", f"/repos/{_forgejo_user}/{mirror}")
  if status != 200:
    log.info(f"webhook registration skipped — mirror {mirror} not found yet")
    return

  # List existing webhooks
  status, hooks_data = _forgejo_api("GET", f"/repos/{_forgejo_user}/{mirror}/hooks")
  if status != 200:
    log.warning(f"failed to list webhooks for {mirror}: {status}")
    return

  for existing in (hooks_data if isinstance(hooks_data, list) else []):
    if isinstance(existing, dict):
      config = existing.get('config', {})
      if isinstance(config, dict) and config.get('url') == webhook_url:
        log.info(f"webhook already registered for {mirror}")
        return

  # Register new webhook
  body = {
    "type": "forgejo",
    "config": {
      "url": webhook_url,
      "content_type": "json",
    },
    "events": ["push"],
    "active": True,
  }
  if cfg.webhook_secret:
    body["config"]["secret"] = cfg.webhook_secret

  status, resp = _forgejo_api("POST", f"/repos/{_forgejo_user}/{mirror}/hooks", body)
  if status in (200, 201):
    log.info(f"webhook registered for {mirror} → {webhook_url}")
  else:
    log.warning(f"failed to register webhook for {mirror}: {status} {resp}")


def _register_all_webhooks(cfg: ProxyConfig):
  for hook in cfg.hooks:
    try:
      _register_webhook_for_hook(cfg, hook)
    except Exception as e:
      log.error(f"error registering webhook for {hook.source_repo}: {e}")


def register_all_webhooks_async(cfg: ProxyConfig):
  t = threading.Thread(target=_register_all_webhooks, args=(cfg,), daemon=True)
  t.start()


# ---------------------------------------------------------------------------
# Webhook signature validation
# ---------------------------------------------------------------------------

def _validate_signature(body: bytes, signature_header: str, secret: str) -> bool:
  expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
  # Forgejo sends "sha256=<hex>" in X-Gitea-Signature (Gitea-compat header)
  prefix = "sha256="
  if not signature_header.startswith(prefix):
    return False
  return hmac.compare_digest(expected, signature_header[len(prefix):])


# ---------------------------------------------------------------------------
# Forgejo tag dereference
# ---------------------------------------------------------------------------

def _resolve_commit_sha(mirror: str, tag_sha: str) -> str:
  """
  Dereference an annotated tag SHA to the underlying commit SHA.
  If the SHA is already a commit (lightweight tag), returns it unchanged.
  """
  _require_init()
  status, data = _forgejo_api("GET", f"/repos/{_forgejo_user}/{mirror}/git/tags/{tag_sha}")
  if status == 200 and isinstance(data, dict):
    obj = data.get('object', {})
    if isinstance(obj, dict) and obj.get('sha'):
      return obj['sha']
  if status != 404:
    log.warning(f"git/tags API returned {status} for {mirror}/{tag_sha}, using original SHA")
  # Lightweight tag (404) or API error — use the original SHA
  return tag_sha


# ---------------------------------------------------------------------------
# Webhook handler
# ---------------------------------------------------------------------------

def handle_forgejo_webhook(body: bytes, headers, cfg: ProxyConfig) -> tuple[int, str]:
  """
  Handle incoming Forgejo push webhook.
  Returns (http_status, message).
  """
  if _forgejo_api is None:
    return 503, "hooks not initialized"

  # Validate signature if secret is configured.
  # Forgejo sends the signature in X-Gitea-Signature (Gitea-compatible header).
  if cfg.webhook_secret:
    sig = headers.get('X-Gitea-Signature') or headers.get('X-Forgejo-Signature') or ''
    if not _validate_signature(body, sig, cfg.webhook_secret):
      log.warning("webhook signature validation failed")
      return 403, "invalid signature"

  try:
    event = json.loads(body)
  except json.JSONDecodeError as e:
    return 400, f"invalid JSON: {e}"
  if not isinstance(event, dict):
    return 400, "payload is not a JSON object"

  ref = event.get('ref', '')
  if not ref.startswith('refs/tags/'):
    return 200, "not a tag push, ignored"

  tag = ref[len('refs/tags/'):]

  repo_obj = event.get('repository')
  full_name = repo_obj.get('full_name', '') if isinstance(repo_obj, dict) else ''
  parsed = _parse_mirror_full_name(full_name)
  if parsed is None:
    log.warning(f"cannot parse mirror full_name: {full_name!r}")
    return 200, "unrecognized repository name format, ignored"

  owner, repo = parsed
  source_repo = f"{owner}/{repo}"

  # Find matching hook
  hook = next((h for h in cfg.hooks if h.source_repo == source_repo), None)
  if hook is None:
    return 200, f"no hook configured for {source_repo}, ignored"

  version = hook.match_tag(tag)
  if version is None:
    return 200, f"tag {tag!r} does not match pattern {hook.tag_pattern!r}, ignored"

  # Resolve commit SHA. Validate it is a real git object name before it flows into
  # the Forgejo API path and the hook command's environment — an unauthenticated
  # webhook (no secret) would otherwise fully control this value.
  tag_sha = event.get('after', '')
  if not isinstance(tag_sha, str) or not _SHA_RE.match(tag_sha) or set(tag_sha) == {'0'}:
    return 400, "missing or invalid 'after' SHA in payload"

  mirror = _mirror_name(owner, repo)
  commit_sha = _resolve_commit_sha(mirror, tag_sha)

  log.info(f"tag {tag} on {source_repo} matched, version={version}, commit={commit_sha}")

  t = threading.Thread(
    target=_run_command_safe,
    args=(hook, tag, version, commit_sha),
    daemon=True,
  )
  t.start()

  return 202, "accepted"


# ---------------------------------------------------------------------------
# Hook command execution
# ---------------------------------------------------------------------------

def _run_command_safe(hook: HookConfig, tag: str, version: str, commit_sha: str):
  try:
    _run_command(hook, tag, version, commit_sha)
  except Exception as e:
    log.error(f"hook command errored for {hook.source_repo} {version}: {e}")


def _run_command(hook: HookConfig, tag: str, version: str, commit_sha: str):
  """
  Run the hook's configured command for a matched release tag.

  The command inherits the proxy's environment plus the hook's own `env`
  entries, plus the GIT_PROXY_* variables describing the release. It runs
  in a fresh temporary working directory (also exported as GIT_PROXY_WORKDIR),
  which is removed afterwards.
  """
  mirror = _mirror_name(hook.owner(), hook.repo())

  env = os.environ.copy()
  env.update(hook.env)
  env.update({
    "GIT_PROXY_SOURCE_REPO": hook.source_repo,
    "GIT_PROXY_SOURCE_URL": hook.source_url(),
    "GIT_PROXY_MIRROR": f"{_forgejo_user}/{mirror}",
    "GIT_PROXY_TAG": tag,
    "GIT_PROXY_VERSION": version,
    "GIT_PROXY_COMMIT_SHA": commit_sha,
  })

  tmpdir = tempfile.mkdtemp(prefix="git-proxy-hook-")
  env["GIT_PROXY_WORKDIR"] = tmpdir
  try:
    # start_new_session=True puts the command in its own process group so that on
    # timeout we can kill the whole tree — for a string command the direct child is
    # /bin/sh, and a plain proc.kill() would orphan anything it backgrounded.
    proc = subprocess.Popen(
      hook.command,
      cwd=tmpdir,
      env=env,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      start_new_session=True,
    )
    try:
      _, stderr = proc.communicate(timeout=hook.timeout)
    except subprocess.TimeoutExpired:
      try:
        os.killpg(proc.pid, signal.SIGKILL)
      except ProcessLookupError:
        pass
      proc.communicate()
      log.error(
        "hook command timed out",
        extra={"repo": hook.source_repo, "version": version, "timeout": hook.timeout},
      )
      return

    if proc.returncode != 0:
      log.error(
        "hook command failed",
        extra={
          "repo": hook.source_repo, "version": version,
          "returncode": proc.returncode,
          "stderr": (stderr or "").strip()[:500],
        },
      )
    else:
      log.info("hook command succeeded", extra={"repo": hook.source_repo, "version": version})
  finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
