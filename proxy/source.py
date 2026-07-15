#!/usr/bin/env python3
"""
Source-repo model for git-proxy.

A single place that defines how an upstream repository (any allowlisted git host,
any path depth) maps to:
  - a canonical key (host/path) used for freshness caching, single-flight dedup,
    logging and hook matching;
  - a deterministic Forgejo mirror name (slug + short hash);
  - the clone/source URL for a given host (honouring per-host base overrides).

proxy.py, config.py and hooks.py all import from here so the mapping lives in one
place — no reverse-parsing of mirror names anywhere.
"""

import hashlib
import os
import re
from dataclasses import dataclass

# A host segment: dns-ish label(s) with an optional :port (e.g. "gitlab.example.com:8443").
_HOST_RE = re.compile(r'^[A-Za-z0-9.\-]+(?::[0-9]+)?$')
# A single repo-path segment (owner / group / subgroup / repo name); same class the
# legacy _NAME_RE used, applied per path segment.
_SEG_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,100}$')

# Known git smart/dumb-HTTP request tails, peeled off the end of a request path to
# recover the repository path of arbitrary depth.
_SERVICE_SUFFIXES = ("/info/refs", "/git-upload-pack", "/git-receive-pack", "/HEAD")


@dataclass(frozen=True)
class SourceRepo:
  """An upstream repository: a host (lowercase, may include :port) and a path of any
  depth (no leading/trailing '/', no trailing '.git'). Frozen so it doubles as a dict
  key and as an equality target in tests."""
  host: str
  path: str

  def key(self) -> str:
    """Canonical identifier `host/path` — the cache/single-flight/log/hook-match key."""
    return f"{self.host}/{self.path}"

  def mirror_name(self) -> str:
    """Deterministic Forgejo repo name: a lossy slug plus a short hash of the exact key.

    The `-{8hex}` suffix makes the name collision-safe across hosts and nesting (the
    hash is of the precise key, not the lossy slug) and structurally avoids Forgejo's
    forbidden names ('.', '..', trailing '.git'/'.wiki'/'.rss'/'.atom' — dots become
    '-'). Length is bounded to ~97 chars."""
    slug = re.sub(r'[^A-Za-z0-9]+', '-', f"{self.host}/{self.path}").strip('-')
    hash8 = hashlib.sha256(self.key().encode()).hexdigest()[:8]
    name = f"{slug[:88]}-{hash8}".strip('-')
    if not name or not name[0].isalnum():
      name = f"m-{name}"
    return name


class SourceRegistry:
  """Allowlist of upstream git hosts, each mapped to a base URL. Built from the
  PROXY_SOURCES env grammar: a comma-separated list of `host` or `host=<baseURL>`
  entries (the latter for a non-default scheme/port on a self-hosted host)."""

  def __init__(self, hosts: dict[str, str]):
    # host (lowercase, may include :port) -> base URL (no trailing slash)
    self._hosts = hosts

  @classmethod
  def from_env(cls, value: str) -> "SourceRegistry":
    hosts: dict[str, str] = {}
    for item in value.split(','):
      item = item.strip()
      if not item:
        continue
      if '=' in item:
        host, base = item.split('=', 1)
        host = host.strip().lower()
        base = base.strip().rstrip('/')
      else:
        host = item.lower()
        base = f"https://{host}"
      if not host:
        continue
      hosts[host] = base
    return cls(hosts)

  def hosts(self) -> list[str]:
    return sorted(self._hosts)

  def is_allowed(self, host: str) -> bool:
    return host.lower() in self._hosts

  def _base(self, host: str) -> str:
    return self._hosts[host.lower()]

  def clone_addr(self, source: SourceRepo) -> str:
    """Upstream clone URL for a source repo (`{base}/{path}.git`)."""
    return f"{self._base(source.host)}/{source.path}.git"

  def source_url(self, source: SourceRepo) -> str:
    """Human/description URL for a source repo — same as clone_addr."""
    return self.clone_addr(source)

  def parse_original_url(self, url) -> "SourceRepo | None":
    """Recover a SourceRepo from a Forgejo `original_url` (set from clone_addr at migration
    time). Matched against each registered host's base URL — the exact inverse of clone_addr
    — so a `host=<baseURL>` override (non-default scheme/port/alias) round-trips correctly.
    Returns None unless some base matches and every path segment is valid."""
    if not isinstance(url, str) or not url:
      return None
    for host, base in self._hosts.items():
      prefix = base + "/"
      if not url.startswith(prefix):
        continue
      path = url[len(prefix):].strip('/').removesuffix('.git')
      if not path:
        return None
      for seg in path.split('/'):
        if seg in ('.', '..') or not _SEG_RE.match(seg):
          return None
      return SourceRepo(host, path)
    return None


def split_git_service(rest: str) -> tuple[str, str]:
  """Split a request-path remainder (everything after the host segment) into
  (repo_path, service), peeling a known git HTTP tail off the end so the repository
  path of any depth is recovered. `service` keeps its leading '/'. If no git service
  tail is recognized, returns ("", rest) — the caller treats an empty repo_path as an
  invalid request."""
  for suffix in _SERVICE_SUFFIXES:
    if rest.endswith(suffix):
      return rest[:-len(suffix)], suffix
  idx = rest.find("/objects/")
  if idx >= 0:
    return rest[:idx], rest[idx:]
  return "", rest


def parse_request_path(path_only: str, registry: SourceRegistry) -> "tuple[SourceRepo, str] | None":
  """Parse `/<host>/<repo-path...>/<git-service>` into (SourceRepo, service).

  Returns None (→ HTTP 400) if fewer than two segments, the host fails _HOST_RE or the
  allowlist, no repo path remains after peeling the git service tail, or any path
  segment is invalid (guards against '.', '..', empty — path traversal)."""
  segs = path_only.lstrip('/').split('/')
  if len(segs) < 2:
    return None
  host = segs[0].lower()
  if not _HOST_RE.match(host) or not registry.is_allowed(host):
    return None
  rest = '/'.join(segs[1:])
  repo_path, service = split_git_service(rest)
  repo_path = repo_path.removesuffix('.git')
  if not repo_path:
    return None
  for seg in repo_path.split('/'):
    if seg in ('.', '..') or not _SEG_RE.match(seg):
      return None
  # Validate the git-service tail too. The fixed suffixes are safe by construction, but the
  # dumb-HTTP `/objects/...` branch is otherwise an arbitrary, attacker-controlled path that
  # gets forwarded to Forgejo under the proxy's admin credentials — reject traversal there.
  for seg in service.split('/'):
    if not seg:
      continue
    if seg in ('.', '..') or not _SEG_RE.match(seg):
      return None
  return SourceRepo(host, repo_path), service


def parse_source_path(text: str, registry: SourceRegistry) -> SourceRepo:
  """Parse a `host/path` source spec from config (no git service tail). Raises
  ValueError if malformed or the host is not in the allowlist — fail-fast at load."""
  segs = text.split('/')
  if len(segs) < 2 or not all(segs):
    raise ValueError(f"source must be 'host/path' with a host and at least one path segment, got: {text!r}")
  host = segs[0].lower()
  if not _HOST_RE.match(host):
    raise ValueError(f"invalid source host {host!r} in {text!r}")
  if not registry.is_allowed(host):
    raise ValueError(f"source host {host!r} not in allowlist (PROXY_SOURCES): {text!r}")
  for seg in segs[1:]:
    if not _SEG_RE.match(seg):
      raise ValueError(f"invalid source path segment {seg!r} in {text!r}")
  return SourceRepo(host, '/'.join(segs[1:]))


# Module-level singleton read by config.py/proxy.py/hooks.py. Tests override
# `source.REGISTRY` (see tests/conftest.py) — modules reference it via `source.REGISTRY`
# so the override is picked up dynamically.
REGISTRY = SourceRegistry.from_env(os.environ.get("PROXY_SOURCES", "github.com"))
