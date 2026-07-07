import hashlib
import hmac
import json
from unittest.mock import patch, MagicMock

import pytest

import hooks
from config import HookConfig, ProxyConfig
from conftest import NoOpThread


# ---------------------------------------------------------------------------
# fixture: isolate hooks module globals across tests
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_hooks_globals():
  saved = (hooks._forgejo_api, hooks._forgejo_user, hooks._listen_port)
  yield
  hooks._forgejo_api, hooks._forgejo_user, hooks._listen_port = saved


def make_hook(**overrides):
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


def make_config(hooks_list=None, webhook_secret=None):
  return ProxyConfig(
    gitlab_url="https://gitlab.example.com",
    gitlab_token="token123",
    gitlab_user="git-user",
    gitlab_email="git-user@example.com",
    conan_common_repo="group/conan-common",
    webhook_secret=webhook_secret,
    hooks=hooks_list if hooks_list is not None else [make_hook()],
  )


# ---------------------------------------------------------------------------
# _mirror_name
# ---------------------------------------------------------------------------

class TestMirrorName:
  def test_combines_owner_and_repo(self):
    assert hooks._mirror_name("Centimo", "simd") == "Centimo__simd"


# ---------------------------------------------------------------------------
# _parse_mirror_full_name
# ---------------------------------------------------------------------------

class TestParseMirrorFullName:
  def test_valid_name(self):
    assert hooks._parse_mirror_full_name("gitadmin/Centimo__simd") == ("Centimo", "simd")

  def test_no_slash_returns_none(self):
    assert hooks._parse_mirror_full_name("Centimo__simd") is None

  def test_no_dunder_returns_none(self):
    assert hooks._parse_mirror_full_name("gitadmin/Centimosimd") is None

  def test_empty_owner_returns_none(self):
    assert hooks._parse_mirror_full_name("gitadmin/__simd") is None

  def test_empty_repo_returns_none(self):
    assert hooks._parse_mirror_full_name("gitadmin/Centimo__") is None

  def test_owner_containing_dunder_splits_on_first_occurrence(self):
    # Known limitation: the "__" delimiter is ambiguous when owner itself
    # contains "__". _parse_mirror_full_name splits on the *first* "__",
    # so "Foo__Bar__baz" is parsed as owner="Foo", repo="Bar__baz" —
    # NOT owner="Foo__Bar", repo="baz".
    assert hooks._parse_mirror_full_name("gitadmin/Foo__Bar__baz") == ("Foo", "Bar__baz")


# ---------------------------------------------------------------------------
# _validate_signature
# ---------------------------------------------------------------------------

class TestValidateSignature:
  def _sign(self, body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

  def test_correct_signature_returns_true(self):
    body = b'{"ref": "refs/tags/v1.0.0"}'
    secret = "supersecret"
    sig = self._sign(body, secret)
    assert hooks._validate_signature(body, sig, secret) is True

  def test_wrong_secret_returns_false(self):
    body = b'{"ref": "refs/tags/v1.0.0"}'
    sig = self._sign(body, "wrong-secret")
    assert hooks._validate_signature(body, sig, "supersecret") is False

  def test_missing_prefix_returns_false(self):
    body = b'{"ref": "refs/tags/v1.0.0"}'
    secret = "supersecret"
    expected_hex = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert hooks._validate_signature(body, expected_hex, secret) is False


# ---------------------------------------------------------------------------
# _resolve_commit_sha
# ---------------------------------------------------------------------------

class TestResolveCommitSha:
  TAG_SHA = "a" * 40
  COMMIT_SHA = "b" * 40

  def test_annotated_tag_dereferenced_to_commit_sha(self):
    api = MagicMock(return_value=(200, {"object": {"sha": self.COMMIT_SHA}}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha("Centimo__simd", self.TAG_SHA)
    assert result == self.COMMIT_SHA
    api.assert_called_once_with("GET", f"/repos/gitadmin/Centimo__simd/git/tags/{self.TAG_SHA}")

  def test_lightweight_tag_404_returns_original_sha(self):
    api = MagicMock(return_value=(404, {}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha("Centimo__simd", self.TAG_SHA)
    assert result == self.TAG_SHA

  def test_api_error_returns_original_sha(self):
    api = MagicMock(return_value=(500, {"message": "internal error"}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha("Centimo__simd", self.TAG_SHA)
    assert result == self.TAG_SHA

  def test_200_without_object_sha_returns_original_sha(self):
    api = MagicMock(return_value=(200, {"object": {}}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha("Centimo__simd", self.TAG_SHA)
    assert result == self.TAG_SHA


# ---------------------------------------------------------------------------
# handle_forgejo_webhook
# ---------------------------------------------------------------------------

class TestHandleForgejoWebhook:
  def test_before_init_returns_503(self):
    # _reset_hooks_globals (autouse) already guarantees hooks.init() has not
    # been called for this test, since it restores the pre-test state on teardown
    # and no earlier test in this module leaves init() called without cleanup.
    assert hooks._forgejo_api is None
    cfg = make_config()
    status, message = hooks.handle_forgejo_webhook(b"{}", {}, cfg)
    assert status == 503

  def test_non_tag_ref_ignored(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "not a tag push" in message

  def test_invalid_json_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    status, message = hooks.handle_forgejo_webhook(b"not-json{{{", {}, cfg)
    assert status == 400

  def test_unparseable_repository_full_name_returns_200(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({
      "ref": "refs/tags/v1.0.0",
      "repository": {"full_name": "no-slash-or-dunder"},
      "after": "a" * 40,
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "unrecognized" in message

  def test_no_hook_configured_for_repo_returns_200(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config(hooks_list=[make_hook(source_repo="Other/repo")])
    body = json.dumps({
      "ref": "refs/tags/v1.0.0",
      "repository": {"full_name": "gitadmin/Centimo__simd"},
      "after": "a" * 40,
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "no hook configured" in message

  def test_tag_not_matching_pattern_returns_200(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config(hooks_list=[make_hook(tag_pattern="v{version}")])
    body = json.dumps({
      "ref": "refs/tags/not-a-semver-tag",
      "repository": {"full_name": "gitadmin/Centimo__simd"},
      "after": "a" * 40,
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "does not match" in message

  def test_missing_after_sha_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({
      "ref": "refs/tags/v1.0.0",
      "repository": {"full_name": "gitadmin/Centimo__simd"},
      "after": "",
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 400

  def test_all_zero_after_sha_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({
      "ref": "refs/tags/v1.0.0",
      "repository": {"full_name": "gitadmin/Centimo__simd"},
      "after": "0" * 40,
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 400

  def test_valid_signature_accepted(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    secret = "supersecret"
    cfg = make_config(webhook_secret=secret)
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-Gitea-Signature": sig}
    status, message = hooks.handle_forgejo_webhook(body, headers, cfg)
    assert status == 200
    assert "not a tag push" in message

  def test_invalid_signature_returns_403(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    secret = "supersecret"
    cfg = make_config(webhook_secret=secret)
    body = json.dumps({"ref": "refs/heads/main"}).encode()
    headers = {"X-Gitea-Signature": "sha256=deadbeef"}
    status, message = hooks.handle_forgejo_webhook(body, headers, cfg)
    assert status == 403

  def test_happy_path_returns_202(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({
      "ref": "refs/tags/v1.2.3",
      "repository": {"full_name": "gitadmin/Centimo__simd"},
      "after": "a" * 40,
    }).encode()

    with patch.object(hooks, "_resolve_commit_sha", return_value="a" * 40), \
         patch("hooks.threading.Thread", NoOpThread):
      status, message = hooks.handle_forgejo_webhook(body, {}, cfg)

    assert status == 202
    assert message == "accepted"
