import io
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

import proxy
from conftest import NoOpThread


# ---------------------------------------------------------------------------
# mirror_name
# ---------------------------------------------------------------------------

class TestMirrorName:
  def test_combines_owner_and_repo(self):
    assert proxy.mirror_name("Centimo", "simd") == "Centimo__simd"


# ---------------------------------------------------------------------------
# _parse_ls_remote
# ---------------------------------------------------------------------------

class TestParseLsRemote:
  def test_parses_heads_and_tags(self):
    output = (
      "aaaa1111\trefs/heads/main\n"
      "bbbb2222\trefs/tags/v1.0.0\n"
    )
    refs = proxy._parse_ls_remote(output)
    assert refs == {
      "refs/heads/main": "aaaa1111",
      "refs/tags/v1.0.0": "bbbb2222",
    }

  def test_ignores_non_heads_non_tags_refs(self):
    output = (
      "aaaa1111\trefs/heads/main\n"
      "cccc3333\trefs/pull/1/head\n"
      "dddd4444\tHEAD\n"
    )
    refs = proxy._parse_ls_remote(output)
    assert refs == {"refs/heads/main": "aaaa1111"}

  def test_ignores_peeled_tags(self):
    output = (
      "bbbb2222\trefs/tags/v1.0.0\n"
      "eeee5555\trefs/tags/v1.0.0^{}\n"
    )
    refs = proxy._parse_ls_remote(output)
    assert refs == {"refs/tags/v1.0.0": "bbbb2222"}

  def test_skips_lines_without_tab(self):
    output = "not-a-valid-line\naaaa1111\trefs/heads/main\n"
    refs = proxy._parse_ls_remote(output)
    assert refs == {"refs/heads/main": "aaaa1111"}

  def test_empty_input_returns_empty_dict(self):
    assert proxy._parse_ls_remote("") == {}


# ---------------------------------------------------------------------------
# pkt_line
# ---------------------------------------------------------------------------

class TestPktLine:
  def test_length_prefix_matches_data(self):
    result = proxy.pkt_line("hello\n")
    # "hello\n" is 6 bytes, total length = 6 + 4 = 10 = 0x000a
    assert result == b"000ahello\n"

  def test_empty_string(self):
    result = proxy.pkt_line("")
    assert result == b"0004"


# ---------------------------------------------------------------------------
# git_error_body
# ---------------------------------------------------------------------------

class TestGitErrorBody:
  def test_contains_service_announcement_flush_and_error(self):
    body = proxy.git_error_body("something went wrong")
    assert b"# service=git-upload-pack" in body
    assert b"0000" in body
    assert b"ERR something went wrong" in body

  def test_structure_order(self):
    body = proxy.git_error_body("boom")
    # service announcement pkt-line, then flush pkt (0000), then ERR pkt-line
    service_line = proxy.pkt_line("# service=git-upload-pack\n")
    err_line = proxy.pkt_line("ERR boom")
    assert body == service_line + b"0000" + err_line


# ---------------------------------------------------------------------------
# _NAME_RE
# ---------------------------------------------------------------------------

class TestNameRe:
  # ".." is formally valid per _NAME_RE (dots are an allowed character), but this is
  # not a path-traversal hole: mirror_name() joins owner/repo with "__" into a single
  # path segment (e.g. "..__.." ), never emitting a literal ".." path segment, and "/"
  # is rejected by the regex so no segment boundary can be injected. Documented, not a bug.
  @pytest.mark.parametrize("name", ["abc", "a.b-c_d", "a" * 100, "..", "a.."])
  def test_valid_names(self, name):
    assert proxy._NAME_RE.match(name) is not None

  def test_empty_string_invalid(self):
    assert proxy._NAME_RE.match("") is None

  def test_too_long_invalid(self):
    assert proxy._NAME_RE.match("a" * 101) is None

  def test_slash_invalid(self):
    assert proxy._NAME_RE.match("a/b") is None

  def test_space_invalid(self):
    assert proxy._NAME_RE.match("a b") is None

  def test_special_char_invalid(self):
    assert proxy._NAME_RE.match("a$b") is None


# ---------------------------------------------------------------------------
# MirrorFreshness.upstream_refs — caching / TTL
# ---------------------------------------------------------------------------

class TestUpstreamRefsCaching:
  def _freshness(self, **kwargs):
    return proxy.MirrorFreshness(
      forgejo_url="http://forgejo.local",
      forgejo_user="gitadmin",
      forgejo_password="pw",
      **kwargs,
    )

  @staticmethod
  def _fake_process(stdout="", returncode=0, stderr=""):
    proc = MagicMock()
    proc.stdout = stdout
    proc.returncode = returncode
    proc.stderr = stderr
    return proc

  def test_second_call_within_ttl_uses_cache(self):
    # _fetch_upstream_refs (not mocked away here) is what actually populates the
    # cache, so we mock subprocess.run instead and let the real caching path run.
    mf = self._freshness(cache_ttl=30)
    fake_output = "sha1\trefs/heads/main\n"
    with patch("proxy.subprocess.run", return_value=self._fake_process(stdout=fake_output)) as mock_run:
      first = mf.upstream_refs("owner", "repo")
      second = mf.upstream_refs("owner", "repo")
    assert first == {"refs/heads/main": "sha1"}
    assert second == {"refs/heads/main": "sha1"}
    mock_run.assert_called_once()

  def test_ttl_expiry_triggers_new_fetch(self):
    mf = self._freshness(cache_ttl=0)
    fake_output = "sha1\trefs/heads/main\n"
    with patch("proxy.subprocess.run", return_value=self._fake_process(stdout=fake_output)) as mock_run:
      mf.upstream_refs("owner", "repo")
      mf.upstream_refs("owner", "repo")
    assert mock_run.call_count == 2

  def test_fetch_error_returns_none(self):
    mf = self._freshness()
    with patch.object(mf, "_fetch_upstream_refs", return_value=None):
      assert mf.upstream_refs("owner", "repo") is None

  def test_different_repos_cached_independently(self):
    mf = self._freshness(cache_ttl=30)
    outputs = ["sha_a\trefs/heads/main\n", "sha_b\trefs/heads/main\n"]
    with patch("proxy.subprocess.run", side_effect=[self._fake_process(stdout=o) for o in outputs]) as mock_run:
      r1 = mf.upstream_refs("owner1", "repo1")
      r2 = mf.upstream_refs("owner2", "repo2")
    assert r1 == {"refs/heads/main": "sha_a"}
    assert r2 == {"refs/heads/main": "sha_b"}
    assert mock_run.call_count == 2

  def test_single_flight_concurrent_real_threads_coalesce_into_one_fetch(self):
    """Two real threads calling upstream_refs for the same repo at the same time
    must coalesce into exactly one subprocess.run call (leader/follower via
    threading.Event, proxy.py:195-227). The fake subprocess.run blocks on a
    barrier until both threads have entered upstream_refs, guaranteeing an
    actual race rather than an accidental serialization."""
    mf = self._freshness(cache_ttl=30)
    entered = threading.Barrier(2, timeout=5)
    release = threading.Event()
    call_count = {"n": 0}
    lock = threading.Lock()

    def fake_run(*args, **kwargs):
      with lock:
        call_count["n"] += 1
      # Block until the test releases us, after confirming both caller threads
      # are inside upstream_refs (one as leader building the request, the other
      # as a follower waiting on the leader's Event).
      release.wait(timeout=5)
      return self._fake_process(stdout="sha1\trefs/heads/main\n")

    results = [None, None]

    def leader_worker():
      entered.wait()
      results[0] = mf.upstream_refs("owner", "repo")

    def follower_worker():
      entered.wait()
      results[1] = mf.upstream_refs("owner", "repo")

    with patch("proxy.subprocess.run", side_effect=fake_run):
      t1 = threading.Thread(target=leader_worker)
      t2 = threading.Thread(target=follower_worker)
      t1.start()
      t2.start()
      # Give both threads a moment to reach upstream_refs and register
      # leader/follower state, then let the fake subprocess.run return.
      time.sleep(0.2)
      release.set()
      t1.join(timeout=5)
      t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert call_count["n"] == 1
    assert results[0] == {"refs/heads/main": "sha1"}
    assert results[1] == {"refs/heads/main": "sha1"}

  def test_single_flight_follower_gets_none_when_leader_fetch_fails(self):
    """If the leader's fetch fails (returns None), the follower must also get
    None promptly — it must not silently serve stale/missing data or hang
    past its wait timeout."""
    mf = self._freshness(cache_ttl=30, ls_remote_timeout=1)
    entered = threading.Barrier(2, timeout=5)
    release = threading.Event()

    def fake_run(*args, **kwargs):
      release.wait(timeout=5)
      return self._fake_process(stdout="", returncode=1, stderr="fatal: could not read")

    results = [None, None]

    def leader_worker():
      entered.wait()
      results[0] = mf.upstream_refs("owner", "repo")

    def follower_worker():
      entered.wait()
      results[1] = mf.upstream_refs("owner", "repo")

    with patch("proxy.subprocess.run", side_effect=fake_run):
      t1 = threading.Thread(target=leader_worker)
      t2 = threading.Thread(target=follower_worker)
      t1.start()
      t2.start()
      time.sleep(0.2)
      release.set()
      t1.join(timeout=5)
      t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert results[0] is None
    assert results[1] is None


# ---------------------------------------------------------------------------
# MirrorFreshness.wait_for_ref_sync — real implementation, only forgejo_refs mocked
# ---------------------------------------------------------------------------

class TestWaitForRefSync:
  def _freshness(self, **kwargs):
    return proxy.MirrorFreshness(
      forgejo_url="http://forgejo.local",
      forgejo_user="gitadmin",
      forgejo_password="pw",
      **kwargs,
    )

  def test_expected_present_immediately_returns_true(self):
    mf = self._freshness()
    expected = {"refs/heads/main": "sha1"}
    with patch.object(mf, "forgejo_refs", return_value=dict(expected)) as mock_refs, \
         patch("proxy.time.sleep") as mock_sleep:
      assert mf.wait_for_ref_sync("owner", "repo", expected, timeout=5) is True
    mock_refs.assert_called_once_with("owner", "repo")
    mock_sleep.assert_not_called()

  def test_converges_after_a_few_polls(self):
    mf = self._freshness()
    expected = {"refs/heads/main": "sha1", "refs/tags/v1.0.0": "sha2"}
    # First poll: only one of the two expected refs present (mirror not yet synced).
    # Second poll: both present.
    side_effects = [
      {"refs/heads/main": "sha1"},
      dict(expected),
    ]
    with patch.object(mf, "forgejo_refs", side_effect=side_effects) as mock_refs, \
         patch("proxy.time.sleep") as mock_sleep:
      assert mf.wait_for_ref_sync("owner", "repo", expected, timeout=5) is True
    assert mock_refs.call_count == 2
    mock_sleep.assert_called_once_with(1.5)

  def test_extra_refs_in_mirror_are_ignored(self):
    mf = self._freshness()
    expected = {"refs/heads/main": "sha1"}
    current = {"refs/heads/main": "sha1", "refs/heads/other": "sha_extra", "refs/tags/v9.9.9": "sha_extra2"}
    with patch.object(mf, "forgejo_refs", return_value=current), \
         patch("proxy.time.sleep") as mock_sleep:
      assert mf.wait_for_ref_sync("owner", "repo", expected, timeout=5) is True
    mock_sleep.assert_not_called()

  def test_never_converges_times_out_false(self):
    mf = self._freshness()
    expected = {"refs/heads/main": "sha_new"}
    stale = {"refs/heads/main": "sha_old"}
    with patch.object(mf, "forgejo_refs", return_value=stale), \
         patch("proxy.time.sleep"):
      assert mf.wait_for_ref_sync("owner", "repo", expected, timeout=0) is False

  def test_forgejo_refs_none_on_some_polls_does_not_crash_and_times_out(self):
    mf = self._freshness()
    expected = {"refs/heads/main": "sha1"}
    # Always None (simulating persistent forgejo ls-remote errors) — must not
    # raise, and must eventually time out rather than hang.
    with patch.object(mf, "forgejo_refs", return_value=None), \
         patch("proxy.time.sleep"):
      assert mf.wait_for_ref_sync("owner", "repo", expected, timeout=0) is False


# ---------------------------------------------------------------------------
# MirrorFreshness.ensure_fresh
# ---------------------------------------------------------------------------

class TestEnsureFresh:
  def _freshness(self):
    return proxy.MirrorFreshness(
      forgejo_url="http://forgejo.local",
      forgejo_user="gitadmin",
      forgejo_password="pw",
    )

  def test_refs_match_returns_true_without_sync(self):
    mf = self._freshness()
    refs = {"refs/heads/main": "sha1"}
    with patch.object(mf, "upstream_refs", return_value=refs), \
         patch.object(mf, "forgejo_refs", return_value=dict(refs)), \
         patch.object(mf, "trigger_sync") as mock_trigger:
      assert mf.ensure_fresh("owner", "repo") is True
    mock_trigger.assert_not_called()

  def test_refs_differ_triggers_sync_and_waits(self):
    mf = self._freshness()
    upstream = {"refs/heads/main": "sha2"}
    current = {"refs/heads/main": "sha1"}
    with patch.object(mf, "upstream_refs", return_value=upstream), \
         patch.object(mf, "forgejo_refs", return_value=current), \
         patch.object(mf, "trigger_sync") as mock_trigger, \
         patch.object(mf, "wait_for_ref_sync", return_value=True) as mock_wait:
      assert mf.ensure_fresh("owner", "repo") is True
    mock_trigger.assert_called_once_with("owner", "repo")
    mock_wait.assert_called_once_with("owner", "repo", upstream)

  def test_refs_differ_and_wait_times_out_returns_false(self):
    mf = self._freshness()
    upstream = {"refs/heads/main": "sha2"}
    current = {"refs/heads/main": "sha1"}
    with patch.object(mf, "upstream_refs", return_value=upstream), \
         patch.object(mf, "forgejo_refs", return_value=current), \
         patch.object(mf, "trigger_sync"), \
         patch.object(mf, "wait_for_ref_sync", return_value=False):
      assert mf.ensure_fresh("owner", "repo") is False

  def test_upstream_error_returns_false_fail_open_no_sync(self):
    mf = self._freshness()
    with patch.object(mf, "upstream_refs", return_value=None), \
         patch.object(mf, "forgejo_refs") as mock_forgejo_refs, \
         patch.object(mf, "trigger_sync") as mock_trigger:
      assert mf.ensure_fresh("owner", "repo") is False
    mock_forgejo_refs.assert_not_called()
    mock_trigger.assert_not_called()

  def test_forgejo_refs_error_returns_false(self):
    mf = self._freshness()
    with patch.object(mf, "upstream_refs", return_value={"refs/heads/main": "sha1"}), \
         patch.object(mf, "forgejo_refs", return_value=None), \
         patch.object(mf, "trigger_sync") as mock_trigger:
      assert mf.ensure_fresh("owner", "repo") is False
    mock_trigger.assert_not_called()


# ---------------------------------------------------------------------------
# MirrorFreshness.trigger_sync — dedup
# ---------------------------------------------------------------------------

class TestTriggerSyncDedup:
  def _freshness(self):
    return proxy.MirrorFreshness(
      forgejo_url="http://forgejo.local",
      forgejo_user="gitadmin",
      forgejo_password="pw",
    )

  def test_first_call_returns_true_second_returns_false_while_in_flight(self):
    mf = self._freshness()

    with patch("proxy.threading.Thread", NoOpThread):
      first = mf.trigger_sync("owner", "repo")
      second = mf.trigger_sync("owner", "repo")

    assert first is True
    assert second is False

  def test_flag_cleared_after_do_sync_completes_allows_retrigger(self):
    mf = self._freshness()
    key = "owner/repo"
    done = threading.Event()
    real_do_sync = mf._do_sync

    def instrumented_do_sync(owner, repo, k):
      try:
        real_do_sync(owner, repo, k)
      finally:
        done.set()

    with patch.object(mf, "_do_sync", side_effect=instrumented_do_sync), \
         patch("proxy.forgejo_api", return_value=(200, {})):
      assert mf.trigger_sync("owner", "repo") is True
      assert done.wait(timeout=2), "background sync thread did not complete in time"
      assert key not in mf._sync_triggered
      assert mf.trigger_sync("owner", "repo") is True


# ---------------------------------------------------------------------------
# ProxyHandler.proxy_to_forgejo — chunked request body reassembly
# ---------------------------------------------------------------------------

def _make_handler(path, command, headers, rfile_bytes):
  """Build a ProxyHandler instance without going through the real socket-based
  __init__ (which requires a live connection). Only the attributes touched by
  proxy_to_forgejo are set."""
  h = proxy.ProxyHandler.__new__(proxy.ProxyHandler)
  h.path = path
  h.command = command
  h.headers = headers
  h.rfile = io.BytesIO(rfile_bytes)
  h.wfile = io.BytesIO()
  return h


def _fake_forgejo_response(status=200, headers=(("Content-Length", "2"),), read_chunks=(b"ok", b"")):
  resp = MagicMock()
  resp.status = status
  resp.headers.items.return_value = list(headers)
  resp.read.side_effect = list(read_chunks)
  resp.__enter__ = MagicMock(return_value=resp)
  resp.__exit__ = MagicMock(return_value=False)
  return resp


class TestProxyToForgejoChunkedReassembly:
  def test_single_chunk_reassembled_correctly(self):
    body = b"hello world"
    # "hello world" is 11 bytes = 0xb
    chunked = b"b\r\nhello world\r\n0\r\n\r\n"
    handler = _make_handler(
      path="/owner/repo/git-upload-pack",
      command="POST",
      headers={"Transfer-Encoding": "chunked"},
      rfile_bytes=chunked,
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    captured = {}

    def fake_urlopen(req, timeout=None):
      captured["req"] = req
      return _fake_forgejo_response()

    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen):
      handler.proxy_to_forgejo("owner", "repo")

    req = captured["req"]
    assert req.data == body
    assert req.headers.get("Content-length") == str(len(body))
    assert "Transfer-encoding" not in req.headers
    assert "Transfer-Encoding" not in req.headers

  def test_multiple_chunks_reassembled_correctly(self):
    part1 = b"hello "
    part2 = b"world"
    part3 = b"!"
    body = part1 + part2 + part3
    chunked = (
      f"{len(part1):x}\r\n".encode() + part1 + b"\r\n"
      + f"{len(part2):x}\r\n".encode() + part2 + b"\r\n"
      + f"{len(part3):x}\r\n".encode() + part3 + b"\r\n"
      + b"0\r\n\r\n"
    )
    handler = _make_handler(
      path="/owner/repo/git-upload-pack",
      command="POST",
      headers={"Transfer-Encoding": "chunked"},
      rfile_bytes=chunked,
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    captured = {}

    def fake_urlopen(req, timeout=None):
      captured["req"] = req
      return _fake_forgejo_response()

    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen):
      handler.proxy_to_forgejo("owner", "repo")

    req = captured["req"]
    assert req.data == body
    assert req.headers.get("Content-length") == str(len(body))


# ---------------------------------------------------------------------------
# ProxyHandler.handle_git_request — orchestration branches
# ---------------------------------------------------------------------------

class TestHandleGitRequest:
  def _handler(self, path, command="GET", headers=None):
    h = proxy.ProxyHandler.__new__(proxy.ProxyHandler)
    h.path = path
    h.command = command
    h.headers = headers if headers is not None else {}
    h.server = MagicMock()
    h.server.freshness = MagicMock()
    h.send_response = MagicMock()
    h.end_headers = MagicMock()
    h.wait_for_mirror = MagicMock()
    h.proxy_to_forgejo = MagicMock()
    h.send_git_error = MagicMock()
    return h

  def test_mirror_missing_creates_and_waits_then_errors_if_still_empty(self):
    handler = self._handler("/owner/repo/info/refs?service=git-upload-pack")
    handler.wait_for_mirror.return_value = None
    with patch.object(proxy, "get_mirror", return_value=None), \
         patch.object(proxy, "create_mirror") as mock_create:
      handler.handle_git_request()
    mock_create.assert_called_once_with("owner", "repo")
    handler.wait_for_mirror.assert_called_once_with("owner", "repo")
    handler.send_git_error.assert_called_once()
    handler.proxy_to_forgejo.assert_not_called()

  def test_mirror_empty_triggers_sync_then_waits(self):
    handler = self._handler("/owner/repo/info/refs?service=git-upload-pack")
    handler.wait_for_mirror.return_value = {"empty": False}
    with patch.object(proxy, "get_mirror", return_value={"empty": True}), \
         patch.object(proxy, "create_mirror") as mock_create:
      handler.handle_git_request()
    handler.server.freshness.trigger_sync.assert_called_once_with("owner", "repo")
    mock_create.assert_not_called()
    handler.wait_for_mirror.assert_called_once_with("owner", "repo")
    handler.proxy_to_forgejo.assert_called_once_with("owner", "repo")

  def test_populated_mirror_info_refs_upload_pack_calls_ensure_fresh(self):
    handler = self._handler(
      "/owner/repo/info/refs?service=git-upload-pack",
      command="GET",
    )
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_fresh.assert_called_once_with("owner", "repo")
    handler.proxy_to_forgejo.assert_called_once_with("owner", "repo")

  def test_populated_mirror_non_info_refs_request_skips_ensure_fresh(self):
    handler = self._handler("/owner/repo/git-upload-pack", command="POST")
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_fresh.assert_not_called()
    handler.proxy_to_forgejo.assert_called_once_with("owner", "repo")

  def test_populated_mirror_get_info_refs_wrong_service_skips_ensure_fresh(self):
    handler = self._handler(
      "/owner/repo/info/refs?service=git-receive-pack",
      command="GET",
    )
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_fresh.assert_not_called()
    handler.proxy_to_forgejo.assert_called_once_with("owner", "repo")

  def test_invalid_owner_repo_rejected_with_400(self):
    handler = self._handler("/a b/x")
    with patch.object(proxy, "get_mirror") as mock_get_mirror:
      handler.handle_git_request()
    mock_get_mirror.assert_not_called()
    handler.send_response.assert_called_once_with(400)
    handler.end_headers.assert_called_once()
    handler.proxy_to_forgejo.assert_not_called()
