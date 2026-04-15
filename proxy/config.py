#!/usr/bin/env python3
"""
Configuration loading for git-proxy hooks.
"""

import os
import re
import logging
from dataclasses import dataclass, field

import yaml

log = logging.getLogger("git-proxy")

_ENV_VAR_RE = re.compile(r'\$\{([A-Za-z_][A-Za-z0-9_]*)\}')


def _expand_env(value: str) -> str:
  return _ENV_VAR_RE.sub(lambda m: os.environ.get(m.group(1), m.group(0)), value)


@dataclass
class HookConfig:
  source_repo: str       # "Centimo/simd"
  tag_pattern: str       # "v{version}"
  base_branch: str       # "main"
  branch_prefix: str     # ""
  branch_suffix: str     # ""
  recipe_path: str       # "conan/conanfile.py"
  _tag_re: re.Pattern = field(init=False, repr=False, compare=False)

  def __post_init__(self):
    parts = self.source_repo.split('/')
    if len(parts) != 2 or not parts[0] or not parts[1]:
      raise ValueError(f"source_repo must be 'owner/repo', got: {self.source_repo!r}")

    # Convert tag_pattern like "v{version}" to regex "^v(?P<version>...)$"
    # Matches SemVer: X.Y.Z with optional pre-release (-alpha.1) and build (+001)
    escaped = re.escape(self.tag_pattern)
    semver = r'(?P<version>[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?(?:\+[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)*)?)'
    pattern = escaped.replace(r'\{version\}', semver)
    self._tag_re = re.compile(f'^{pattern}$')

  def owner(self) -> str:
    return self.source_repo.split('/')[0]

  def repo(self) -> str:
    return self.source_repo.split('/')[1]

  def match_tag(self, tag: str) -> str | None:
    """Returns version string if tag matches tag_pattern, else None."""
    m = self._tag_re.match(tag)
    if m:
      return m.group('version')
    return None

  def branch_name(self, version: str) -> str:
    return f"{self.branch_prefix}{self.repo()}-{version}{self.branch_suffix}"

  def package_ref(self, version: str) -> str:
    return f"{self.repo()}/{version}"

  def github_url(self) -> str:
    return f"https://github.com/{self.source_repo}.git"


@dataclass
class ProxyConfig:
  gitlab_url: str
  gitlab_token: str
  gitlab_user: str
  gitlab_email: str
  conan_common_repo: str
  webhook_secret: str | None
  hooks: list[HookConfig]


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

  hooks_raw = raw.get('hooks', [])
  if not hooks_raw:
    return None

  def require(d: dict, key: str, context: str) -> str:
    val = d.get(key)
    if not val:
      raise ValueError(f"Missing required field '{key}' in {context}")
    return _expand_env(str(val))

  hooks = []
  for i, h in enumerate(hooks_raw):
    ctx = f"hooks[{i}]"
    hooks.append(HookConfig(
      source_repo=require(h, 'source_repo', ctx),
      tag_pattern=require(h, 'tag_pattern', ctx),
      base_branch=require(h, 'base_branch', ctx),
      branch_prefix=_expand_env(str(h.get('branch_prefix', ''))),
      branch_suffix=_expand_env(str(h.get('branch_suffix', ''))),
      recipe_path=require(h, 'recipe_path', ctx),
    ))

  secret = raw.get('webhook_secret')
  if secret is not None and not isinstance(secret, str):
    raise ValueError(f"webhook_secret must be a string, got: {type(secret).__name__}")
  return ProxyConfig(
    gitlab_url=require(raw, 'gitlab_url', 'root'),
    gitlab_token=require(raw, 'gitlab_token', 'root'),
    gitlab_user=require(raw, 'gitlab_user', 'root'),
    gitlab_email=require(raw, 'gitlab_email', 'root'),
    conan_common_repo=require(raw, 'conan_common_repo', 'root'),
    webhook_secret=_expand_env(str(secret)) if secret else None,
    hooks=hooks,
  )
