#!/usr/bin/env python3
"""
Webhook handling and git workflow for git-proxy.

Receives Forgejo push events for mirrored repos, detects release tags,
and creates branches in conan-common with an updated packages.yml.
"""

import hashlib
import hmac
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.parse

import yaml

from config import HookConfig, ProxyConfig

log = logging.getLogger("git-proxy")

WEBHOOK_PATH = "/webhook/forgejo"
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

  ref = event.get('ref', '')
  if not ref.startswith('refs/tags/'):
    return 200, "not a tag push, ignored"

  tag = ref[len('refs/tags/'):]

  full_name = event.get('repository', {}).get('full_name', '')
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

  # Resolve commit SHA
  tag_sha = event.get('after', '')
  if not tag_sha or set(tag_sha) == {'0'}:
    return 400, "missing or zero 'after' SHA in payload"

  mirror = _mirror_name(owner, repo)
  commit_sha = _resolve_commit_sha(mirror, tag_sha)

  log.info(f"tag {tag} on {source_repo} matched, version={version}, commit={commit_sha}")

  t = threading.Thread(
    target=_git_workflow_safe,
    args=(cfg, hook, version, commit_sha),
    daemon=True,
  )
  t.start()

  return 202, "accepted"


# ---------------------------------------------------------------------------
# Git workflow
# ---------------------------------------------------------------------------

class GitError(Exception):
  pass


def run_git(*args, cwd: str, env: dict | None = None, timeout: int = 120) -> str:
  result = subprocess.run(
    ["git", *args],
    cwd=cwd,
    env=env,
    capture_output=True,
    text=True,
    timeout=timeout,
  )
  if result.returncode != 0:
    raise GitError(result.stderr.strip())
  return result.stdout.strip()


def _git_workflow_safe(cfg: ProxyConfig, hook: HookConfig, version: str, commit_sha: str):
  try:
    _git_workflow(cfg, hook, version, commit_sha)
  except Exception as e:
    log.error(f"git workflow failed for {hook.source_repo} {version}: {e}")


def _make_git_env(cfg: ProxyConfig, tmpdir: str) -> tuple[str, dict]:
  """
  Build a git clone URL and env with credentials stored in a netrc file
  inside tmpdir to avoid leaking the token in process args or git config.
  Returns (clone_url_without_creds, env_dict).
  """
  parsed = urllib.parse.urlparse(cfg.gitlab_url)
  host = parsed.netloc  # e.g. "gitlab.example.com"
  scheme = parsed.scheme

  netrc_path = os.path.join(tmpdir, ".netrc")
  fd = os.open(netrc_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
  with os.fdopen(fd, 'w') as f:
    f.write(f"machine {host}\n")
    f.write(f"login {cfg.gitlab_user}\n")
    f.write(f"password {cfg.gitlab_token}\n")

  clone_url = f"{scheme}://{host}/{cfg.conan_common_repo}.git"

  env = os.environ.copy()
  env["GIT_CONFIG_COUNT"] = "1"
  env["GIT_CONFIG_KEY_0"] = "credential.helper"
  env["GIT_CONFIG_VALUE_0"] = ""
  env["HOME"] = tmpdir  # git reads ~/.netrc from HOME

  return clone_url, env


def _git_workflow(cfg: ProxyConfig, hook: HookConfig, version: str, commit_sha: str):
  branch = hook.branch_name(version)
  package_ref = hook.package_ref(version)

  tmpdir = tempfile.mkdtemp(prefix="git-proxy-hook-")
  try:
    clone_url, git_env = _make_git_env(cfg, tmpdir)
    workdir = os.path.join(tmpdir, "conan-common")

    log.info(f"cloning {cfg.conan_common_repo} branch {hook.base_branch}")
    run_git(
      "clone", "--depth=1", "--branch", hook.base_branch,
      clone_url, workdir,
      cwd=tmpdir, env=git_env, timeout=120,
    )

    run_git("config", "user.email", cfg.gitlab_email, cwd=workdir, env=git_env)
    run_git("config", "user.name", cfg.gitlab_user, cwd=workdir, env=git_env)

    # Check if branch already exists remotely
    result = subprocess.run(
      ["git", "ls-remote", "--heads", "origin", branch],
      cwd=workdir, env=git_env, capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
      raise GitError(f"ls-remote failed: {result.stderr.strip()}")
    if result.stdout.strip():
      log.info(f"branch {branch} already exists in {cfg.conan_common_repo}, skipping")
      return

    run_git("checkout", "-b", branch, cwd=workdir, env=git_env)

    # Update packages.yml
    packages_yml_path = os.path.join(workdir, "packages.yml")
    if os.path.exists(packages_yml_path):
      with open(packages_yml_path) as f:
        data = yaml.safe_load(f) or {}
    else:
      data = {}

    packages = data.get('packages', [])
    if not isinstance(packages, list):
      packages = []

    # Check if package_ref already present
    for entry in packages:
      if isinstance(entry, dict) and entry.get('package_ref') == package_ref:
        log.info(f"{package_ref} already in packages.yml, skipping")
        return

    new_entry = {
      'package_ref': package_ref,
      'repo': hook.github_url(),
      'ref': commit_sha,
      'recipe_path': hook.recipe_path,
    }
    packages.append(new_entry)
    data['packages'] = packages

    with open(packages_yml_path, 'w') as f:
      yaml.dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

    run_git("add", "packages.yml", cwd=workdir, env=git_env)
    run_git("commit", "-m", f"Add {package_ref}", cwd=workdir, env=git_env)
    run_git("push", "origin", branch, cwd=workdir, env=git_env, timeout=120)

    log.info(f"pushed branch {branch} to {cfg.conan_common_repo} with {package_ref}")

  finally:
    shutil.rmtree(tmpdir, ignore_errors=True)
