#!/usr/bin/env python3
"""
Configuration loading for git-proxy hooks.
"""

import os
import re
import logging
from dataclasses import dataclass, field

import yaml

import source
from source import SourceRepo, parse_source_path

log = logging.getLogger("git-proxy")

_ENV_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')

DEFAULT_HOOK_TIMEOUT = 300  # seconds before a hook command is killed


def _expand_env(value: str) -> str:
  return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


@dataclass
class HookConfig:
  source: str             # "github.com/Centimo/simd" — host/path on the upstream (any depth)
  tag_pattern: str        # "v{version}" — {version} expands to a SemVer regex group
  command: list[str]      # argv passed to subprocess (already normalized, no shell involved)
  env: dict[str, str]     # extra environment variables exported to the command
  timeout: int            # seconds before the command is killed
  _tag_re: re.Pattern = field(init=False, repr=False, compare=False)
  _source: SourceRepo = field(init=False, repr=False, compare=False)

  def __post_init__(self):
    # Validate host (allowlist, fail-fast) + path segments, and cache the parsed source.
    self._source = parse_source_path(self.source, source.REGISTRY)

    # Convert tag_pattern like "v{version}" to regex "^v(?P<version>...)$"
    # Matches SemVer: X.Y.Z with optional pre-release (-alpha.1) and build (+001)
    escaped = re.escape(self.tag_pattern)
    semver = r'(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?(?:\+[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?)'
    pattern = escaped.replace(r'\{version\}', semver)
    self._tag_re = re.compile(f'^{pattern}$')

  def source_repo(self) -> SourceRepo:
    return self._source

  def match_tag(self, tag: str) -> str | None:
    """Returns version string if tag matches tag_pattern, else None."""
    m = self._tag_re.match(tag)
    if m:
      return m.group('version')
    return None

  def source_url(self) -> str:
    return source.REGISTRY.source_url(self._source)


@dataclass
class ProxyConfig:
  webhook_secret: str | None
  hooks: list[HookConfig]


def _parse_command(raw, ctx: str) -> list[str]:
  """
  Normalize the `command` field into an argv list ready for subprocess.run.

  - A string is run through the shell: ["/bin/sh", "-c", <string>].
  - A list is used as argv verbatim (no shell).
  Both forms have ${VAR} expanded from the environment.
  """
  if isinstance(raw, str):
    cmd = _expand_env(raw)
    if not cmd.strip():
      raise ValueError(f"'command' is empty in {ctx}")
    return ["/bin/sh", "-c", cmd]
  if isinstance(raw, list):
    if not raw:
      raise ValueError(f"'command' list is empty in {ctx}")
    argv = []
    for part in raw:
      if not isinstance(part, (str, int, float)) or isinstance(part, bool):
        raise ValueError(f"'command' list items must be strings in {ctx}, got: {part!r}")
      argv.append(_expand_env(str(part)))
    if not argv[0].strip():
      raise ValueError(f"'command' program (first list item) is empty in {ctx}")
    return argv
  raise ValueError(f"'command' must be a string or a list of strings in {ctx}")


def _parse_env(raw, ctx: str) -> dict[str, str]:
  if not raw:
    return {}
  if not isinstance(raw, dict):
    raise ValueError(f"'env' must be a mapping in {ctx}")
  out = {}
  for k, v in raw.items():
    key = str(k)
    if not key or '=' in key or '\0' in key:
      raise ValueError(f"invalid env var name {key!r} in {ctx}")
    out[key] = _expand_env(str(v))
  return out


def _parse_timeout(raw, ctx: str) -> int:
  if raw is None:
    return DEFAULT_HOOK_TIMEOUT
  if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
    raise ValueError(f"'timeout' must be a positive integer in {ctx}, got: {raw!r}")
  return raw


def load_config(path: str) -> ProxyConfig | None:
  """
  Load hook configuration from a YAML file.
  Returns None if the file does not exist or contains no hooks.
  Raises ValueError on malformed config.
  """
  if not os.path.exists(path):
    return None

  with open(path) as f:
    raw = yaml.safe_load(f)

  if not raw:
    return None

  if not isinstance(raw, dict):
    raise ValueError("config root must be a mapping")

  hooks_raw = raw.get('hooks', [])
  if not hooks_raw:
    return None
  if not isinstance(hooks_raw, list):
    raise ValueError("'hooks' must be a list")

  def require(d: dict, key: str, context: str) -> str:
    val = d.get(key)
    if not val:
      raise ValueError(f"Missing required field '{key}' in {context}")
    return _expand_env(str(val))

  hooks = []
  for i, h in enumerate(hooks_raw):
    ctx = f"hooks[{i}]"
    if not isinstance(h, dict):
      raise ValueError(f"{ctx} must be a mapping")
    if 'command' not in h:
      raise ValueError(f"Missing required field 'command' in {ctx}")
    hooks.append(HookConfig(
      source=require(h, 'source', ctx),
      tag_pattern=require(h, 'tag_pattern', ctx),
      command=_parse_command(h.get('command'), ctx),
      env=_parse_env(h.get('env'), ctx),
      timeout=_parse_timeout(h.get('timeout'), ctx),
    ))

  secret = raw.get('webhook_secret')
  if secret is not None and not isinstance(secret, str):
    raise ValueError(f"webhook_secret must be a string, got: {type(secret).__name__}")
  return ProxyConfig(
    webhook_secret=_expand_env(str(secret)) if secret else None,
    hooks=hooks,
  )
