import pytest

from config import HookConfig, load_config, _expand_env


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

class TestMatchTag:
  def _hook(self, tag_pattern="v{version}"):
    return HookConfig(
      source_repo="owner/repo",
      tag_pattern=tag_pattern,
      base_branch="main",
      branch_prefix="",
      branch_suffix="",
      recipe_path="conan/conanfile.py",
    )

  def test_matches_plain_semver(self):
    hook = self._hook()
    assert hook.match_tag("v1.2.3") == "1.2.3"

  def test_matches_prerelease(self):
    hook = self._hook()
    assert hook.match_tag("v1.2.3-alpha.1") == "1.2.3-alpha.1"

  def test_matches_build_metadata(self):
    hook = self._hook()
    assert hook.match_tag("v1.2.3+build.5") == "1.2.3+build.5"

  def test_rejects_incomplete_semver(self):
    hook = self._hook()
    assert hook.match_tag("v1.2") is None

  def test_rejects_wrong_prefix(self):
    hook = self._hook()
    assert hook.match_tag("release-1.2.3") is None

  def test_other_pattern_prefix(self):
    hook = self._hook(tag_pattern="release-{version}")
    assert hook.match_tag("release-1.2.3") == "1.2.3"
    assert hook.match_tag("v1.2.3") is None

  def test_anchored_rejects_extra_prefix(self):
    hook = self._hook()
    assert hook.match_tag("xv1.2.3") is None

  def test_anchored_rejects_extra_suffix(self):
    hook = self._hook()
    assert hook.match_tag("v1.2.3x") is None


# ---------------------------------------------------------------------------
# HookConfig field helpers
# ---------------------------------------------------------------------------

class TestHookConfigHelpers:
  def _hook(self, **overrides):
    defaults = dict(
      source_repo="Centimo/simd",
      tag_pattern="v{version}",
      base_branch="main",
      branch_prefix="",
      branch_suffix="",
      recipe_path="conan/conanfile.py",
    )
    defaults.update(overrides)
    return HookConfig(**defaults)

  def test_owner(self):
    assert self._hook().owner() == "Centimo"

  def test_repo(self):
    assert self._hook().repo() == "simd"

  def test_branch_name_without_prefix_suffix(self):
    assert self._hook().branch_name("1.2.3") == "simd-1.2.3"

  def test_branch_name_with_prefix_and_suffix(self):
    hook = self._hook(branch_prefix="auto/", branch_suffix="-release")
    assert hook.branch_name("1.2.3") == "auto/simd-1.2.3-release"

  def test_package_ref(self):
    assert self._hook().package_ref("1.2.3") == "simd/1.2.3"

  def test_github_url(self):
    assert self._hook().github_url() == "https://github.com/Centimo/simd.git"


# ---------------------------------------------------------------------------
# HookConfig.__post_init__ validation
# ---------------------------------------------------------------------------

class TestHookConfigValidation:
  def _make(self, source_repo):
    return HookConfig(
      source_repo=source_repo,
      tag_pattern="v{version}",
      base_branch="main",
      branch_prefix="",
      branch_suffix="",
      recipe_path="conan/conanfile.py",
    )

  def test_no_slash_raises(self):
    with pytest.raises(ValueError):
      self._make("ownerrepo")

  def test_empty_owner_raises(self):
    with pytest.raises(ValueError):
      self._make("/repo")

  def test_empty_repo_raises(self):
    with pytest.raises(ValueError):
      self._make("owner/")

  def test_valid_owner_repo_ok(self):
    hook = self._make("owner/repo")
    assert hook.owner() == "owner"
    assert hook.repo() == "repo"


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------

VALID_CONFIG = """
gitlab_url: https://gitlab.example.com
gitlab_token: token123
gitlab_user: git-user
gitlab_email: git-user@example.com
conan_common_repo: group/conan-common
webhook_secret: supersecret
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    base_branch: main
    recipe_path: conan/conanfile.py
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
    cfg_file.write_text("""
gitlab_url: https://gitlab.example.com
gitlab_token: token123
gitlab_user: git-user
gitlab_email: git-user@example.com
conan_common_repo: group/conan-common
""")
    assert load_config(str(cfg_file)) is None

  def test_empty_hooks_list_returns_none(self, tmp_path):
    cfg_file = tmp_path / "empty_hooks.yml"
    cfg_file.write_text("""
gitlab_url: https://gitlab.example.com
gitlab_token: token123
gitlab_user: git-user
gitlab_email: git-user@example.com
conan_common_repo: group/conan-common
hooks: []
""")
    assert load_config(str(cfg_file)) is None

  def test_valid_full_config(self, tmp_path):
    cfg_file = tmp_path / "valid.yml"
    cfg_file.write_text(VALID_CONFIG)
    cfg = load_config(str(cfg_file))
    assert cfg is not None
    assert cfg.gitlab_url == "https://gitlab.example.com"
    assert cfg.gitlab_token == "token123"
    assert cfg.gitlab_user == "git-user"
    assert cfg.gitlab_email == "git-user@example.com"
    assert cfg.conan_common_repo == "group/conan-common"
    assert cfg.webhook_secret == "supersecret"
    assert len(cfg.hooks) == 1
    hook = cfg.hooks[0]
    assert hook.source_repo == "Centimo/simd"
    assert hook.tag_pattern == "v{version}"
    assert hook.base_branch == "main"
    assert hook.recipe_path == "conan/conanfile.py"
    assert hook.branch_prefix == ""
    assert hook.branch_suffix == ""

  @pytest.mark.parametrize("missing_field", [
    "gitlab_url", "gitlab_token", "gitlab_user", "gitlab_email", "conan_common_repo",
  ])
  def test_missing_required_root_field_raises(self, tmp_path, missing_field):
    root_fields = {
      "gitlab_url": "https://gitlab.example.com",
      "gitlab_token": "token123",
      "gitlab_user": "git-user",
      "gitlab_email": "git-user@example.com",
      "conan_common_repo": "group/conan-common",
    }
    del root_fields[missing_field]
    lines = [f"{k}: {v}" for k, v in root_fields.items()]
    lines.append("hooks:")
    lines.append("  - source_repo: Centimo/simd")
    lines.append('    tag_pattern: "v{version}"')
    lines.append("    base_branch: main")
    lines.append("    recipe_path: conan/conanfile.py")
    cfg_file = tmp_path / "missing_root.yml"
    cfg_file.write_text("\n".join(lines))
    with pytest.raises(ValueError, match=f"Missing required field '{missing_field}'"):
      load_config(str(cfg_file))

  @pytest.mark.parametrize("missing_field", [
    "source_repo", "tag_pattern", "base_branch", "recipe_path",
  ])
  def test_missing_required_hook_field_raises(self, tmp_path, missing_field):
    hook_fields = {
      "source_repo": "Centimo/simd",
      "tag_pattern": "v{version}",
      "base_branch": "main",
      "recipe_path": "conan/conanfile.py",
    }
    del hook_fields[missing_field]
    root = (
      "gitlab_url: https://gitlab.example.com\n"
      "gitlab_token: token123\n"
      "gitlab_user: git-user\n"
      "gitlab_email: git-user@example.com\n"
      "conan_common_repo: group/conan-common\n"
      "hooks:\n"
    )
    hook_lines = "\n".join(f"    {k}: {v}" for k, v in hook_fields.items())
    cfg_file = tmp_path / "missing_hook_field.yml"
    cfg_file.write_text(root + "  - " + hook_lines.lstrip() + "\n")
    with pytest.raises(ValueError, match=f"Missing required field '{missing_field}'"):
      load_config(str(cfg_file))

  def test_non_string_webhook_secret_raises(self, tmp_path):
    cfg_file = tmp_path / "bad_secret.yml"
    cfg_file.write_text("""
gitlab_url: https://gitlab.example.com
gitlab_token: token123
gitlab_user: git-user
gitlab_email: git-user@example.com
conan_common_repo: group/conan-common
webhook_secret: 12345
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    base_branch: main
    recipe_path: conan/conanfile.py
""")
    with pytest.raises(ValueError):
      load_config(str(cfg_file))

  def test_env_var_expansion_in_values(self, tmp_path, monkeypatch):
    monkeypatch.setenv("GITLAB_TOKEN", "secret-from-env")
    monkeypatch.setenv("BRANCH_PREFIX", "auto/")
    cfg_file = tmp_path / "with_env.yml"
    cfg_file.write_text("""
gitlab_url: https://gitlab.example.com
gitlab_token: "${GITLAB_TOKEN}"
gitlab_user: git-user
gitlab_email: git-user@example.com
conan_common_repo: group/conan-common
hooks:
  - source_repo: Centimo/simd
    tag_pattern: "v{version}"
    base_branch: main
    recipe_path: conan/conanfile.py
    branch_prefix: "${BRANCH_PREFIX}"
""")
    cfg = load_config(str(cfg_file))
    assert cfg is not None
    assert cfg.gitlab_token == "secret-from-env"
    assert cfg.hooks[0].branch_prefix == "auto/"
