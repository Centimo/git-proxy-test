import hashlib
import hmac
import json
import subprocess
from unittest.mock import patch, MagicMock

import pytest

import hooks
from config import HookConfig, ProxyConfig, DEFAULT_HOOK_TIMEOUT
from source import SourceRepo
from conftest import NoOpThread


# Mirror name for the default hook source, used where the webhook payload's full_name matters.
SIMD = SourceRepo("github.com", "Centimo/simd")
SIMD_MIRROR = SIMD.mirror_name()
SIMD_FULL_NAME = f"gitadmin/{SIMD_MIRROR}"
SIMD_ORIGINAL_URL = "https://github.com/Centimo/simd.git"


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
    source="github.com/Centimo/simd",
    tag_pattern="v{version}",
    command=["true"],
    env={},
    timeout=DEFAULT_HOOK_TIMEOUT,
  )
  defaults.update(overrides)
  return HookConfig(**defaults)


def make_config(hooks_list=None, webhook_secret=None):
  return ProxyConfig(
    webhook_secret=webhook_secret,
    hooks=hooks_list if hooks_list is not None else [make_hook()],
  )


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
  FULL_NAME = SIMD_FULL_NAME

  def test_annotated_tag_dereferenced_to_commit_sha(self):
    api = MagicMock(return_value=(200, {"object": {"sha": self.COMMIT_SHA}}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha(self.FULL_NAME, self.TAG_SHA)
    assert result == self.COMMIT_SHA
    api.assert_called_once_with("GET", f"/repos/{self.FULL_NAME}/git/tags/{self.TAG_SHA}")

  def test_lightweight_tag_404_returns_original_sha(self):
    api = MagicMock(return_value=(404, {}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha(self.FULL_NAME, self.TAG_SHA)
    assert result == self.TAG_SHA

  def test_api_error_returns_original_sha(self):
    api = MagicMock(return_value=(500, {"message": "internal error"}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha(self.FULL_NAME, self.TAG_SHA)
    assert result == self.TAG_SHA

  def test_200_without_object_sha_returns_original_sha(self):
    api = MagicMock(return_value=(200, {"object": {}}))
    hooks.init(api, "gitadmin", 8080)
    result = hooks._resolve_commit_sha(self.FULL_NAME, self.TAG_SHA)
    assert result == self.TAG_SHA


# ---------------------------------------------------------------------------
# handle_forgejo_webhook
# ---------------------------------------------------------------------------

def _payload(ref="refs/tags/v1.2.3", full_name=SIMD_FULL_NAME, original_url=SIMD_ORIGINAL_URL, after="a" * 40, **extra):
  repo = {"full_name": full_name}
  if original_url is not None:
    repo["original_url"] = original_url
  body = {"ref": ref, "repository": repo, "after": after}
  body.update(extra)
  return json.dumps(body).encode()


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

  def test_missing_original_url_and_no_fallback_returns_200(self):
    # No original_url in payload; fallback GET /repos/<full_name> also yields no original_url.
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = _payload(original_url=None)
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "unrecognized" in message

  def test_original_url_missing_uses_get_fallback(self):
    # Payload lacks original_url, but the fallback GET /repos/<full_name> supplies it.
    api = MagicMock(return_value=(200, {"original_url": SIMD_ORIGINAL_URL}))
    hooks.init(api, "gitadmin", 8080)
    cfg = make_config()
    body = _payload(original_url=None)
    with patch.object(hooks, "_resolve_commit_sha", return_value="a" * 40), \
         patch("hooks.threading.Thread", NoOpThread):
      status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 202
    api.assert_called_once_with("GET", f"/repos/{SIMD_FULL_NAME}")

  def test_non_allowlisted_original_url_ignored(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = _payload(original_url="https://bitbucket.org/a/b.git")
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "unrecognized source" in message

  def test_null_repository_ignored_not_crash(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({
      "ref": "refs/tags/v1.0.0",
      "repository": None,
      "after": "a" * 40,
    }).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "unrecognized" in message

  def test_no_hook_configured_for_repo_returns_200(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config(hooks_list=[make_hook(source="github.com/Other/repo")])
    body = _payload()  # original_url points at Centimo/simd
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "no hook configured" in message

  def test_tag_not_matching_pattern_returns_200(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config(hooks_list=[make_hook(tag_pattern="v{version}")])
    body = _payload(ref="refs/tags/not-a-semver-tag")
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "does not match" in message

  def test_missing_after_sha_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = _payload(after="")
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 400

  def test_all_zero_after_sha_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = _payload(after="0" * 40)
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 400

  def test_non_hex_after_sha_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = _payload(after="; rm -rf / #" + "a" * 28)  # 40 chars, not hex
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 400

  def test_non_object_payload_returns_400(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    status, message = hooks.handle_forgejo_webhook(b'[1, 2, 3]', {}, cfg)
    assert status == 400

  def test_null_ref_ignored_not_crash(self):
    # "ref": null must not raise (None.startswith) — treated as a non-tag push.
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    cfg = make_config()
    body = json.dumps({"ref": None, "repository": {"full_name": SIMD_FULL_NAME}}).encode()
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "not a tag push" in message

  def test_traversal_full_name_not_fetched_from_api(self):
    # No original_url in payload; a traversal-shaped full_name must NOT be sent to the
    # token-authed Forgejo API in the fallback GET.
    api = MagicMock(return_value=(200, {}))
    hooks.init(api, "gitadmin", 8080)
    cfg = make_config()
    body = _payload(full_name="x/../../admin/users", original_url=None)
    status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 200
    assert "unrecognized" in message
    api.assert_not_called()

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
    body = _payload()

    with patch.object(hooks, "_resolve_commit_sha", return_value="a" * 40) as resolve, \
         patch("hooks.threading.Thread", NoOpThread):
      status, message = hooks.handle_forgejo_webhook(body, {}, cfg)

    assert status == 202
    assert message == "accepted"
    # _resolve_commit_sha must be given the payload's authoritative full_name.
    assert resolve.call_args[0][0] == SIMD_FULL_NAME

  def test_nested_gitlab_source_matched(self):
    hooks.init(MagicMock(return_value=(200, {})), "gitadmin", 8080)
    src = SourceRepo("gitlab.com", "group/sub/conan-common")
    cfg = make_config(hooks_list=[make_hook(source="gitlab.com/group/sub/conan-common")])
    body = _payload(
      full_name=f"gitadmin/{src.mirror_name()}",
      original_url="https://gitlab.com/group/sub/conan-common.git",
    )
    with patch.object(hooks, "_resolve_commit_sha", return_value="a" * 40), \
         patch("hooks.threading.Thread", NoOpThread):
      status, message = hooks.handle_forgejo_webhook(body, {}, cfg)
    assert status == 202


# ---------------------------------------------------------------------------
# _run_command
# ---------------------------------------------------------------------------

class TestRunCommand:
  def _fake_proc(self, returncode=0, timeout=False):
    proc = MagicMock()
    proc.pid = 4321
    proc.returncode = returncode
    if timeout:
      # First call (with a timeout=) raises; the post-kill reap call (no timeout) returns.
      def communicate(timeout=None):
        if timeout is not None:
          raise subprocess.TimeoutExpired(cmd="x", timeout=timeout)
        return ("", "")
      proc.communicate.side_effect = communicate
    else:
      proc.communicate.return_value = ("", "")
    return proc

  def _run(self, hook, tag="v1.2.3", version="1.2.3", commit="c" * 40, returncode=0):
    hooks.init(MagicMock(), "gitadmin", 8080)
    proc = self._fake_proc(returncode=returncode)
    with patch("hooks.subprocess.Popen", return_value=proc) as popen, \
         patch("hooks.tempfile.mkdtemp", return_value="/tmp/hook-xyz"), \
         patch("hooks.shutil.rmtree") as rmtree:
      hooks._run_command(hook, tag, version, commit)
    return popen, rmtree

  def test_runs_command_with_argv_and_workdir(self):
    hook = make_hook(command=["python3", "handle.py"])
    popen, rmtree = self._run(hook)
    args, kwargs = popen.call_args
    assert args[0] == ["python3", "handle.py"]
    assert kwargs["cwd"] == "/tmp/hook-xyz"
    assert kwargs["start_new_session"] is True
    rmtree.assert_called_once_with("/tmp/hook-xyz", ignore_errors=True)

  def test_injects_git_proxy_env_vars(self):
    hook = make_hook(source="github.com/Centimo/simd")
    popen, _ = self._run(hook, tag="v2.0.0", version="2.0.0", commit="d" * 40)
    env = popen.call_args.kwargs["env"]
    assert env["GIT_PROXY_SOURCE_REPO"] == "github.com/Centimo/simd"
    assert env["GIT_PROXY_SOURCE_HOST"] == "github.com"
    assert env["GIT_PROXY_SOURCE_URL"] == "https://github.com/Centimo/simd.git"
    assert env["GIT_PROXY_MIRROR"] == f"gitadmin/{SIMD_MIRROR}"
    assert env["GIT_PROXY_TAG"] == "v2.0.0"
    assert env["GIT_PROXY_VERSION"] == "2.0.0"
    assert env["GIT_PROXY_COMMIT_SHA"] == "d" * 40
    assert env["GIT_PROXY_WORKDIR"] == "/tmp/hook-xyz"

  def test_injects_git_proxy_env_vars_nested_gitlab(self):
    src = SourceRepo("gitlab.com", "group/sub/repo")
    hook = make_hook(source="gitlab.com/group/sub/repo")
    popen, _ = self._run(hook)
    env = popen.call_args.kwargs["env"]
    assert env["GIT_PROXY_SOURCE_REPO"] == "gitlab.com/group/sub/repo"
    assert env["GIT_PROXY_SOURCE_HOST"] == "gitlab.com"
    assert env["GIT_PROXY_SOURCE_URL"] == "https://gitlab.com/group/sub/repo.git"
    assert env["GIT_PROXY_MIRROR"] == f"gitadmin/{src.mirror_name()}"

  def test_hook_env_merged_into_command_env(self):
    hook = make_hook(env={"TARGET_REPO": "group/conan-common"})
    popen, _ = self._run(hook)
    assert popen.call_args.kwargs["env"]["TARGET_REPO"] == "group/conan-common"

  def test_custom_timeout_passed_to_communicate(self):
    hook = make_hook(timeout=45)
    hooks.init(MagicMock(), "gitadmin", 8080)
    proc = self._fake_proc()
    with patch("hooks.subprocess.Popen", return_value=proc), \
         patch("hooks.tempfile.mkdtemp", return_value="/tmp/hook-t"), \
         patch("hooks.shutil.rmtree"):
      hooks._run_command(hook, "v1.0.0", "1.0.0", "a" * 40)
    proc.communicate.assert_called_with(timeout=45)

  def test_timeout_kills_process_group_and_cleans_up(self):
    hook = make_hook()
    hooks.init(MagicMock(), "gitadmin", 8080)
    proc = self._fake_proc(timeout=True)
    with patch("hooks.subprocess.Popen", return_value=proc), \
         patch("hooks.tempfile.mkdtemp", return_value="/tmp/hook-fail"), \
         patch("hooks.os.killpg") as killpg, \
         patch("hooks.shutil.rmtree") as rmtree:
      # must not raise
      hooks._run_command(hook, "v1.0.0", "1.0.0", "e" * 40)
    killpg.assert_called_once_with(4321, hooks.signal.SIGKILL)
    rmtree.assert_called_once_with("/tmp/hook-fail", ignore_errors=True)

  def test_run_command_safe_swallows_exceptions(self):
    hook = make_hook()
    hooks.init(MagicMock(), "gitadmin", 8080)
    with patch("hooks._run_command", side_effect=RuntimeError("boom")):
      # must not raise
      hooks._run_command_safe(hook, "v1.0.0", "1.0.0", "f" * 40)
