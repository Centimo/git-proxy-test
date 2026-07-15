import pytest

from config import HookConfig, load_config, _expand_env, DEFAULT_HOOK_TIMEOUT


# ---------------------------------------------------------------------------
# _expand_env
# ---------------------------------------------------------------------------

class TestExpandEnv:
  def test_expands_single_var(self, monkeypatch):
    monkeypatch.setenv("MY_VAR", "hello")
    assert _expand_env("${MY_VAR}") == "hello"

  def test_expands_multiple_vars_in_one_string(self, monkeypatch):
    monkeypatch.setenv("HOST", "example.com")
    monkeypatch.setenv("PORT", "8080")
    assert _expand_env("http://${HOST}:${PORT}/path") == "http://example.com:8080/path"

  def test_missing_var_left_untouched(self, monkeypatch):
    monkeypatch.delenv("DOES_NOT_EXIST", raising=False)
    assert _expand_env("${DOES_NOT_EXIST}") == "${DOES_NOT_EXIST}"

  def test_string_without_vars_unchanged(self):
    assert _expand_env("plain string, no vars") == "plain string, no vars"

  def test_mixed_present_and_missing(self, monkeypatch):
    monkeypatch.setenv("PRESENT", "yes")
    monkeypatch.delenv("MISSING", raising=False)
    assert _expand_env("${PRESENT}-${MISSING}") == "yes-${MISSING}"


# ---------------------------------------------------------------------------
# HookConfig.match_tag
# ---------------------------------------------------------------------------

def _hook(**overrides):
  defaults = dict(
    source_repo="owner/repo",
    tag_pattern="v{version}",
    command=["true"],
    env={},
    timeout=DEFAULT_HOOK_TIMEOUT,
  )
  defaults.update(overrides)
  return HookConfig(**defaults)


class TestMatchTag:
  def test_matches_plain_semver(self):
    assert _hook().match_tag("v1.2.3") == "1.2.3"

  def test_matches_prerelease(self):
    assert _hook().match_tag("v1.2.3-alpha.1") == "1.2.3-alpha.1"

  def test_matches_build_metadata(self):
    assert _hook().match_tag("v1.2.3+build.5") == "1.2.3+build.5"

  def test_rejects_incomplete_semver(self):
    assert _hook().match_tag("v1.2") is None

  def test_rejects_wrong_prefix(self):
    assert _hook().match_tag("release-1.2.3") is None

  def test_other_pattern_prefix(self):
    hook = _hook(tag_pattern="release-{version}")
    assert hook.match_tag("release-1.2.3") == "1.2.3"
    assert hook.match_tag("v1.2.3") is None

  def test_anchored_rejects_extra_prefix(self):
    assert _hook().match_tag("xv1.2.3") is None

  def test_anchored_rejects_extra_suffix(self):
    assert _hook().match_tag("v1.2.3x") is None


# ---------------------------------------------------------------------------
# HookConfig field helpers
# ---------------------------------------------------------------------------

class TestHookConfigHelpers:
  def test_owner(self):
    assert _hook(source_repo="Centimo/simd").owner() == "Centimo"

  def test_repo(self):
    assert _hook(source_repo="Centimo/simd").repo() == "simd"

  def test_source_url(self):
    assert _hook(source_repo="Centimo/simd").source_url() == "https://github.com/Centimo/simd.git"


# ---------------------------------------------------------------------------
# HookConfig.__post_init__ validation
# ---------------------------------------------------------------------------

class TestHookConfigValidation:
  def test_no_slash_raises(self):
    with pytest.raises(ValueError):
      _hook(source_repo="ownerrepo")

  def test_empty_owner_raises(self):
    with pytest.raises(ValueError):
      _hook(source_repo="/repo")

  def test_empty_repo_raises(self):
    with pytest.raises(ValueError):
      _hook(source_repo="owner/")

  def test_valid_owner_repo_ok(self):
    hook = _hook(source_repo="owner/repo")
    assert hook.owner() == "owner"
    assert hook.repo() == "repo"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

VALID_CONFIG = """
webhook_secret: supersecret
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
"""


class TestLoadConfig:
  def test_nonexistent_file_returns_none(self, tmp_path):
    missing = tmp_path / "does-not-exist.yml"
    assert load_config(str(missing)) is None

  def test_empty_file_returns_none(self, tmp_path):
    empty = tmp_path / "empty.yml"
    empty.write_text("")
    assert load_config(str(empty)) is None

  def test_no_hooks_key_returns_none(self, tmp_path):
    cfg_file = tmp_path / "no_hooks.yml"
    cfg_file.write_text("webhook_secret: s\n")
    assert load_config(str(cfg_file)) is None

  def test_empty_hooks_list_returns_none(self, tmp_path):
    cfg_file = tmp_path / "empty_hooks.yml"
    cfg_file.write_text("hooks: []\n")
    assert load_config(str(cfg_file)) is None

  def test_valid_full_config(self, tmp_path):
    cfg_file = tmp_path / "valid.yml"
    cfg_file.write_text(VALID_CONFIG)
    cfg = load_config(str(cfg_file))
    assert cfg is not None
    assert cfg.webhook_secret == "supersecret"
    assert len(cfg.hooks) == 1
    hook = cfg.hooks[0]
    assert hook.source_repo == "Centimo/simd"
    assert hook.tag_pattern == "v{version}"
    # string command → wrapped in a shell
    assert hook.command == ["/bin/sh", "-c", "./handle.sh"]
    assert hook.env == {}
    assert hook.timeout == DEFAULT_HOOK_TIMEOUT

  def test_no_webhook_secret_is_none(self, tmp_path):
    cfg_file = tmp_path / "no_secret.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: ["./handle.sh"]
""")
    cfg = load_config(str(cfg_file))
    assert cfg is not None
    assert cfg.webhook_secret is None

  def test_list_command_used_verbatim(self, tmp_path):
    cfg_file = tmp_path / "list_cmd.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: ["python3", "handle.py", "--flag"]
""")
    cfg = load_config(str(cfg_file))
    assert cfg.hooks[0].command == ["python3", "handle.py", "--flag"]

  def test_env_and_timeout_parsed(self, tmp_path):
    cfg_file = tmp_path / "env_timeout.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
    timeout: 45
    env:
      TARGET_REPO: group/conan-common
      RETRIES: 3
""")
    hook = load_config(str(cfg_file)).hooks[0]
    assert hook.timeout == 45
    assert hook.env == {"TARGET_REPO": "group/conan-common", "RETRIES": "3"}

  @pytest.mark.parametrize("missing_field", ["source_repo", "tag_pattern", "command"])
  def test_missing_required_hook_field_raises(self, tmp_path, missing_field):
    hook_fields = {
      "source_repo": "Centimo/simd",
      "tag_pattern": '"v{version}"',
      "command": '"./handle.sh"',
    }
    del hook_fields[missing_field]
    lines = ["hooks:"]
    items = list(hook_fields.items())
    for idx, (k, v) in enumerate(items):
      prefix = "  - " if idx == 0 else "    "
      lines.append(f"{prefix}{k}: {v}")
    cfg_file = tmp_path / "missing_hook_field.yml"
    cfg_file.write_text("\n".join(lines) + "\n")
    with pytest.raises(ValueError, match=f"'{missing_field}'"):
      load_config(str(cfg_file))

  def test_empty_string_command_raises(self, tmp_path):
    cfg_file = tmp_path / "empty_cmd.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "   "
""")
    with pytest.raises(ValueError, match="empty"):
      load_config(str(cfg_file))

  def test_empty_list_command_raises(self, tmp_path):
    cfg_file = tmp_path / "empty_list_cmd.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: []
""")
    with pytest.raises(ValueError, match="'command'"):
      load_config(str(cfg_file))

  def test_empty_argv_program_raises(self, tmp_path):
    cfg_file = tmp_path / "empty_prog.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: ["", "arg"]
""")
    with pytest.raises(ValueError, match="program"):
      load_config(str(cfg_file))

  def test_invalid_env_key_raises(self, tmp_path):
    cfg_file = tmp_path / "bad_env.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
    env:
      "BAD=KEY": value
""")
    with pytest.raises(ValueError, match="env var name"):
      load_config(str(cfg_file))

  def test_hooks_not_a_list_raises(self, tmp_path):
    cfg_file = tmp_path / "hooks_map.yml"
    cfg_file.write_text("""
hooks:
  source_repo: Centimo/simd
  tag_pattern: "v{version}"
  command: "./handle.sh"
""")
    with pytest.raises(ValueError, match="'hooks' must be a list"):
      load_config(str(cfg_file))

  def test_hook_element_not_a_mapping_raises(self, tmp_path):
    cfg_file = tmp_path / "hook_scalar.yml"
    cfg_file.write_text("""
hooks:
  - just-a-string
""")
    with pytest.raises(ValueError, match="must be a mapping"):
      load_config(str(cfg_file))

  def test_non_positive_timeout_raises(self, tmp_path):
    cfg_file = tmp_path / "bad_timeout.yml"
    cfg_file.write_text("""
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
    timeout: 0
""")
    with pytest.raises(ValueError, match="timeout"):
      load_config(str(cfg_file))

  def test_non_string_webhook_secret_raises(self, tmp_path):
    cfg_file = tmp_path / "bad_secret.yml"
    cfg_file.write_text("""
webhook_secret: 12345
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
""")
    with pytest.raises(ValueError):
      load_config(str(cfg_file))

  def test_env_var_expansion_in_values(self, tmp_path, monkeypatch):
    monkeypatch.setenv("WEBHOOK_SECRET", "secret-from-env")
    monkeypatch.setenv("GITLAB_TOKEN", "tok-from-env")
    cfg_file = tmp_path / "with_env.yml"
    cfg_file.write_text("""
webhook_secret: "${WEBHOOK_SECRET}"
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    command: "./handle.sh"
    env:
      TOKEN: "${GITLAB_TOKEN}"
""")
    cfg = load_config(str(cfg_file))
    assert cfg is not None
    assert cfg.webhook_secret == "secret-from-env"
    assert cfg.hooks[0].env == {"TOKEN": "tok-from-env"}
