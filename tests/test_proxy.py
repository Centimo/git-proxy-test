import io
import threading
import time
from unittest.mock import patch, MagicMock

import pytest

import proxy
import source
from source import SourceRepo, SourceRegistry, parse_request_path
from conftest import NoOpThread


SRC = SourceRepo("github.com", "owner/repo")


# ---------------------------------------------------------------------------
# SourceRepo.mirror_name
# ---------------------------------------------------------------------------

class TestMirrorName:
  def test_deterministic(self):
    assert SRC.mirror_name() == SourceRepo("github.com", "owner/repo").mirror_name()

  def test_has_eight_hex_suffix(self):
    name = SourceRepo("github.com", "Centimo/simd").mirror_name()
    head, _, suffix = name.rpartition("-")
    assert head
    assert len(suffix) == 8
    assert all(c in "0123456789abcdef" for c in suffix)

  def test_bounded_length(self):
    long = SourceRepo("gitlab.example.com", "/".join(["segment"] * 40))
    assert len(long.mirror_name()) <= 100

  def test_starts_with_alphanumeric(self):
    # slug derives only from alnum + '-', hash suffix is hex → first char alnum.
    assert SourceRepo("github.com", "a/b").mirror_name()[0].isalnum()

  def test_distinct_for_host_and_nesting(self):
    a = SourceRepo("github.com", "a/b").mirror_name()
    b = SourceRepo("gitlab.com", "a/b").mirror_name()
    c = SourceRepo("github.com", "a/b/c").mirror_name()
    assert len({a, b, c}) == 3


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
# parse_request_path (replaces the old _NAME_RE tests)
# ---------------------------------------------------------------------------

class TestParseRequestPath:
  def _reg(self):
    return SourceRegistry.from_env("github.com,gitlab.com,gitlab.example.com:8443")

  def test_two_segment_github(self):
    src, service = parse_request_path("/github.com/owner/repo/info/refs", self._reg())
    assert src == SourceRepo("github.com", "owner/repo")
    assert service == "/info/refs"

  def test_nested_gitlab_subgroups(self):
    src, service = parse_request_path("/gitlab.com/group/sub/deep/repo/info/refs", self._reg())
    assert src == SourceRepo("gitlab.com", "group/sub/deep/repo")
    assert service == "/info/refs"

  def test_dot_git_suffix_stripped(self):
    src, service = parse_request_path("/gitlab.com/group/sub/repo.git/git-upload-pack", self._reg())
    assert src == SourceRepo("gitlab.com", "group/sub/repo")
    assert service == "/git-upload-pack"

  def test_non_allowlisted_host_returns_none(self):
    assert parse_request_path("/bitbucket.org/a/b/info/refs", self._reg()) is None

  def test_dotdot_segment_returns_none(self):
    assert parse_request_path("/github.com/../etc/info/refs", self._reg()) is None

  def test_single_dot_segment_returns_none(self):
    assert parse_request_path("/github.com/./repo/info/refs", self._reg()) is None

  def test_empty_segment_returns_none(self):
    assert parse_request_path("/github.com/owner//info/refs", self._reg()) is None

  def test_only_host_returns_none(self):
    assert parse_request_path("/github.com", self._reg()) is None

  def test_no_repo_after_service_returns_none(self):
    # host + service tail but no repo path in between
    assert parse_request_path("/github.com/info/refs", self._reg()) is None

  def test_no_recognized_service_returns_none(self):
    assert parse_request_path("/github.com/owner/repo", self._reg()) is None

  def test_host_with_port(self):
    src, service = parse_request_path("/gitlab.example.com:8443/g/s/r/info/refs", self._reg())
    assert src == SourceRepo("gitlab.example.com:8443", "g/s/r")
    assert service == "/info/refs"

  def test_receive_pack_tail(self):
    src, service = parse_request_path("/github.com/o/r/git-receive-pack", self._reg())
    assert src == SourceRepo("github.com", "o/r")
    assert service == "/git-receive-pack"

  def test_objects_dumb_http_tail(self):
    src, service = parse_request_path("/github.com/o/r/objects/12/abcdef", self._reg())
    assert src == SourceRepo("github.com", "o/r")
    assert service == "/objects/12/abcdef"

  def test_objects_tail_traversal_returns_none(self):
    # The /objects/ tail is otherwise unvalidated and forwarded to Forgejo under admin auth;
    # a '..' segment in it must be rejected.
    assert parse_request_path("/github.com/o/r/objects/../../../x/info/refs", self._reg()) is None

  def test_bad_char_in_segment_returns_none(self):
    assert parse_request_path("/github.com/ow ner/repo/info/refs", self._reg()) is None


# ---------------------------------------------------------------------------
# SourceRegistry.parse_original_url — symmetric with clone_addr
# ---------------------------------------------------------------------------

class TestParseOriginalUrl:
  def test_default_host_roundtrip(self):
    reg = SourceRegistry.from_env("github.com")
    src = SourceRepo("github.com", "Centimo/simd")
    assert reg.parse_original_url(reg.clone_addr(src)) == src

  def test_nested_gitlab_roundtrip(self):
    reg = SourceRegistry.from_env("gitlab.com")
    src = SourceRepo("gitlab.com", "group/sub/repo")
    assert reg.parse_original_url(reg.clone_addr(src)) == src

  def test_base_override_with_port_roundtrips(self):
    # host key has no port, base URL does — recovery must still match (symmetric with clone_addr).
    reg = SourceRegistry.from_env("gitlab.example.com=https://gitlab.example.com:8443")
    src = SourceRepo("gitlab.example.com", "g/r")
    addr = reg.clone_addr(src)
    assert addr == "https://gitlab.example.com:8443/g/r.git"
    assert reg.parse_original_url(addr) == src

  def test_non_allowlisted_returns_none(self):
    reg = SourceRegistry.from_env("github.com")
    assert reg.parse_original_url("https://bitbucket.org/a/b.git") is None

  def test_traversal_in_path_returns_none(self):
    reg = SourceRegistry.from_env("github.com")
    assert reg.parse_original_url("https://github.com/a/../../etc.git") is None

  def test_empty_and_non_str_return_none(self):
    reg = SourceRegistry.from_env("github.com")
    assert reg.parse_original_url("") is None
    assert reg.parse_original_url(None) is None


# ---------------------------------------------------------------------------
# _is_upload_pack_request(service, command, query)
# ---------------------------------------------------------------------------

class TestIsUploadPack:
  def test_get_info_refs_upload_pack(self):
    assert proxy._is_upload_pack_request("/info/refs", "GET", "service=git-upload-pack") is True

  def test_get_info_refs_receive_pack(self):
    assert proxy._is_upload_pack_request("/info/refs", "GET", "service=git-receive-pack") is False

  def test_get_info_refs_no_service(self):
    assert proxy._is_upload_pack_request("/info/refs", "GET", "") is False

  def test_post_git_upload_pack(self):
    assert proxy._is_upload_pack_request("/git-upload-pack", "POST", "") is True

  def test_post_git_receive_pack(self):
    assert proxy._is_upload_pack_request("/git-receive-pack", "POST", "") is False

  def test_get_git_upload_pack_service_is_not_upload_pack(self):
    # A GET on /git-upload-pack (not the info/refs advertisement) is not the fetch POST.
    assert proxy._is_upload_pack_request("/git-upload-pack", "GET", "") is False


# ---------------------------------------------------------------------------
# MirrorFreshness.ensure_synced — sync-on-demand: freshness cache, wait/async modes,
# single-flight dedup, fail-open
# ---------------------------------------------------------------------------

class TestEnsureSynced:
  def _freshness(self, **kwargs):
    kwargs.setdefault("sync_mode", "wait")
    return proxy.MirrorFreshness(
      forgejo_url="http://forgejo.local",
      forgejo_user="gitadmin",
      forgejo_password="pw",
      **kwargs,
    )

  def test_cache_hit_second_call_within_ttl_skips_sync(self):
    mf = self._freshness(freshness_ttl=30)
    with patch("proxy.forgejo_api", return_value=(200, {})) as mock_api, \
         patch("proxy.time.sleep"):
      # First call: prev mirror_updated fetch (GET) + sync (POST) + poll (GET, changed value).
      mock_api.side_effect = [
        (200, {"mirror_updated": "t0"}),   # prev
        (200, {}),                          # mirror-sync POST
        (200, {"mirror_updated": "t1"}),   # poll: changed → converged
      ]
      first = mf.ensure_synced(SRC)
      mock_api.side_effect = None
      mock_api.reset_mock()
      second = mf.ensure_synced(SRC)
    assert first is True
    assert second is True
    mock_api.assert_not_called()

  def test_cache_expired_triggers_sync_again(self):
    mf = self._freshness(freshness_ttl=0)
    with patch("proxy.forgejo_api") as mock_api, \
         patch("proxy.time.sleep"):
      mock_api.side_effect = [
        (200, {"mirror_updated": "t0"}),
        (200, {}),
        (200, {"mirror_updated": "t1"}),
      ]
      first = mf.ensure_synced(SRC)
      mock_api.side_effect = [
        (200, {"mirror_updated": "t1"}),
        (200, {}),
        (200, {"mirror_updated": "t2"}),
      ]
      second = mf.ensure_synced(SRC)
    assert first is True
    assert second is True

  def test_wait_mode_polls_until_mirror_updated_changes_then_true(self):
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=30)
    with patch.object(mf, "_get_mirror_updated", side_effect=[(True, "t0"), (True, "t0"), (True, "t1")]) as mock_get, \
         patch("proxy.forgejo_api", return_value=(200, {})) as mock_api, \
         patch("proxy.time.sleep") as mock_sleep:
      result = mf.ensure_synced(SRC)
    assert result is True
    mock_api.assert_called_once()
    assert mock_api.call_args[0][0] == "POST"
    assert "mirror-sync" in mock_api.call_args[0][1]
    assert mock_get.call_count == 3  # prev + 2 polls
    mock_sleep.assert_called()

  def test_poll_bounds_http_timeout_by_remaining_deadline(self):
    """Each poll's _get_mirror_updated must be called with a timeout bounded by the time left
    until the deadline (min of FORGEJO_API_TIMEOUT and remaining), so a single slow HTTP call
    can't overrun sync_wait_timeout by up to a full FORGEJO_API_TIMEOUT."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=5)
    poll_timeouts = []

    def fake_get(source_repo, timeout=proxy.FORGEJO_API_TIMEOUT):
      poll_timeouts.append(timeout)
      # prev-snapshot (1st call) then converge on the first poll.
      return (True, "t0") if len(poll_timeouts) == 1 else (True, "t1")

    with patch.object(mf, "_get_mirror_updated", side_effect=fake_get), \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch("proxy.time.sleep"):
      result = mf.ensure_synced(SRC)
    assert result is True
    # First call is the pre-snapshot (default timeout); the poll call (2nd) must carry a
    # bounded timeout: positive and no larger than both the remaining budget and the cap.
    poll_timeout = poll_timeouts[1]
    assert 0 < poll_timeout <= proxy.FORGEJO_API_TIMEOUT
    assert poll_timeout <= 5  # cannot exceed sync_wait_timeout

  def test_wait_mode_times_out_returns_false_and_logs_error(self):
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=0)
    with patch.object(mf, "_get_mirror_updated", return_value=(True, "t0")), \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch("proxy.time.sleep"), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_log_error.assert_called()
    assert mf._is_fresh(SRC.key()) is False

  def test_async_mode_returns_true_immediately_without_marking_fresh_synchronously(self):
    """async mode must return control to the caller fast, WITHOUT waiting for the background
    sync to complete. Critically, the mirror must NOT be marked fresh synchronously — the
    background thread is blocked (via a controlled Event) until after we've asserted this, so
    if ensure_synced's return had (incorrectly) already marked fresh, this test would catch it."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30, sync_wait_timeout=5)
    bg_may_proceed = threading.Event()
    get_calls = {"n": 0}

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        get_calls["n"] += 1
        # First GET (pre-snapshot, before POST) returns "t0"; every GET after that (in the
        # poll loop, which only runs once the background thread is released) returns "t1" so
        # the background thread converges promptly instead of spinning to its own timeout.
        return 200, {"mirror_updated": "t0" if get_calls["n"] == 1 else "t1"}
      # POST: block the background thread here until the test says so.
      bg_may_proceed.wait(timeout=5)
      return 200, {}

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      result = mf.ensure_synced(SRC)
      # ensure_synced returned already; the background thread is still blocked on the POST.
      assert result is True
      assert mf._is_fresh(SRC.key()) is False, \
        "must not be marked fresh synchronously — only the background thread may do that, on real completion"
      bg_may_proceed.set()
      # Give the background thread a moment to finish (poll _ensure_inflight instead of sleep;
      # this loop's own time.sleep is the REAL one — only proxy.time.sleep is mocked above).
      deadline = time.monotonic() + 5
      while mf._ensure_inflight and time.monotonic() < deadline:
        time.sleep(0.01)
    assert mf._ensure_inflight == {}
    assert mf._is_fresh(SRC.key()) is True, "background thread must mark fresh once it genuinely converges"

  def test_async_mode_background_sync_failure_does_not_mark_fresh(self):
    """If the background sync fails (mirror-sync POST returns non-200), the mirror must NOT
    be marked fresh once the background thread finishes — a later request within freshness_ttl
    must still see it as not fresh and be able to retry the sync."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30, sync_wait_timeout=5)

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        return 200, {"mirror_updated": "t0"}
      return 500, {"message": "boom"}  # mirror-sync POST fails

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      result = mf.ensure_synced(SRC)
      assert result is True  # async still returns True fast regardless of eventual outcome
      deadline = time.monotonic() + 5
      while mf._ensure_inflight and time.monotonic() < deadline:
        time.sleep(0.01)

    assert mf._ensure_inflight == {}
    assert mf._is_fresh(SRC.key()) is False, "a failed background sync must not mark the mirror fresh"

  def test_async_mode_background_sync_timeout_does_not_mark_fresh(self):
    """If the background sync's poll loop times out (mirror_updated never converges), the
    mirror must NOT be marked fresh."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30, sync_wait_timeout=0.001)

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        return 200, {"mirror_updated": "t0"}  # never changes -> never converges
      return 200, {}

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
      assert result is True
      deadline = time.monotonic() + 5
      while mf._ensure_inflight and time.monotonic() < deadline:
        time.sleep(0.01)

    assert mf._ensure_inflight == {}
    assert mf._is_fresh(SRC.key()) is False
    mock_log_error.assert_called()

  def test_async_mode_background_thread_exception_does_not_crash_and_clears_inflight(self):
    """An unexpected exception inside the background thread (e.g. _mark_fresh raising) must
    not propagate (it's a daemon thread — an uncaught exception there would just be logged by
    Python's default excepthook and silently leave bookkeeping stuck) and must still release
    _ensure_inflight[key] so a later call isn't stuck forever."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30, sync_wait_timeout=5)
    get_calls = {"n": 0}

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        get_calls["n"] += 1
        # First GET = pre-snapshot ("t0"); subsequent GETs = poll, already converged ("t1").
        return 200, {"mirror_updated": "t0" if get_calls["n"] == 1 else "t1"}
      return 200, {}

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch.object(mf, "_mark_fresh", side_effect=RuntimeError("boom")), \
         patch("proxy.time.sleep"), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
      assert result is True
      deadline = time.monotonic() + 5
      while mf._ensure_inflight and time.monotonic() < deadline:
        time.sleep(0.01)

    assert mf._ensure_inflight == {}, "background thread must clear inflight even if it raises internally"
    mock_log_error.assert_called()

  def test_async_mode_thread_start_failure_releases_inflight_and_fails_open(self):
    """If the background sync thread cannot even be started (e.g. RuntimeError under thread/fd
    exhaustion), the leader must release the in-flight slot itself — otherwise the background
    body's finally never runs, the key stays stuck forever, and this repo silently stops being
    synced (every later async caller sees is_leader=False and returns True without syncing)."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30)
    with patch("proxy.threading.Thread") as mock_thread, \
         patch.object(proxy.log, "error") as mock_log_error:
      mock_thread.return_value.start.side_effect = RuntimeError("can't start new thread")
      result = mf.ensure_synced(SRC)

    assert result is False, "thread-start failure must fail open (False), not raise"
    assert mf._ensure_inflight == {}, "in-flight slot must be released so a later call can retry"
    assert mf._is_fresh(SRC.key()) is False, "must not be marked fresh — no sync happened"
    mock_log_error.assert_called()

    # A subsequent call must be able to become leader again (key was released).
    with patch("proxy.threading.Thread") as mock_thread2:
      mf.ensure_synced(SRC)
      mock_thread2.return_value.start.assert_called_once()

  def test_fail_open_api_exception_returns_false_no_raise(self):
    mf = self._freshness(sync_mode="wait", freshness_ttl=30)
    with patch.object(mf, "_get_mirror_updated", return_value=(True, "t0")), \
         patch("proxy.forgejo_api", side_effect=RuntimeError("boom")), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_log_error.assert_called()

  def test_fail_open_non_200_returns_false_no_raise(self):
    mf = self._freshness(sync_mode="wait", freshness_ttl=30)
    with patch.object(mf, "_get_mirror_updated", return_value=(True, "t0")), \
         patch("proxy.forgejo_api", return_value=(500, {"message": "boom"})), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_log_error.assert_called()

  def test_fail_open_network_exception_during_prev_mirror_updated_fetch(self):
    """_get_mirror_updated (used for the pre-sync snapshot, not mocked away here) must
    itself swallow a forgejo_api exception (e.g. connection refused / DNS failure) rather
    than letting it propagate out of ensure_synced."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30)
    with patch("proxy.forgejo_api", side_effect=OSError("connection refused")), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_log_error.assert_called()

  def test_leader_bookkeeping_cleared_after_completion_allows_resync(self):
    """ensure_synced runs the sync inline (not via a background threading.Thread like
    trigger_sync), so single-flight dedup across concurrent callers is exercised with real
    threads below. This test verifies leader/follower bookkeeping (_ensure_inflight) is
    cleared after a sync completes, so a later call (post-TTL) can trigger a fresh sync
    rather than being stuck thinking one is still in flight."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=0)
    with patch.object(mf, "_get_mirror_updated", side_effect=[(True, "t0"), (True, "t1")]), \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch("proxy.time.sleep"):
      assert mf.ensure_synced(SRC) is True
    assert mf._ensure_inflight == {}

  # -------------------------------------------------------------------------
  # BUG 1 regression: prev_updated is None/empty (mirror never synced before)
  # must not cause a false-positive convergence on the very first poll.
  # -------------------------------------------------------------------------

  def test_prev_updated_none_does_not_falsely_converge_on_first_poll(self):
    """Freshly created mirror: mirror_updated is legitimately empty (never synced before —
    Forgejo reports it as ""). This test mocks forgejo_api directly (not _get_mirror_updated),
    so it exercises the REAL _get_mirror_updated → _run_sync convergence check end to end.
    A buggy implementation using `current_updated is not None and current_updated != prev_updated`
    would treat prev="" / current="" as "not None and unchanged"... but treats prev=None /
    current="" as "not None and CHANGED" — a false positive on the very first poll, since
    None (missing field) and "" (empty field) are different Python values despite both meaning
    "never synced". The correct implementation must keep polling until a genuinely non-empty
    value appears, and time out (False) if that never happens within sync_wait_timeout."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=0.001)
    # Pre-snapshot: field absent entirely (None). Every poll: field present but empty ("").
    # A real Forgejo mirror that has never synced could plausibly report either shape; the
    # combination here is exactly what triggers the historical bug (prev=None, current="").
    responses = iter([{"mirror_updated": None}] + [{"mirror_updated": ""}] * 10)
    def fake_api(method, path, *args, **kwargs):
      if method == "POST":
        return 200, {}
      return 200, next(responses)
    with patch("proxy.forgejo_api", side_effect=fake_api) as mock_api, \
         patch("proxy.time.sleep"), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    assert mock_api.call_count >= 2  # prev-snapshot GET + POST, at least one poll GET
    mock_log_error.assert_called()
    assert mf._is_fresh(SRC.key()) is False

  def test_prev_updated_none_converges_true_once_a_non_empty_value_appears(self):
    """Same starting point (mirror never synced, mirror_updated absent/empty) but the sync
    actually completes and mirror_updated becomes non-empty on a later poll — must converge
    True. Exercises the real _get_mirror_updated via forgejo_api, not a mocked one."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=30)
    get_responses = iter([
      {"mirror_updated": None},   # prev: never synced
      {"mirror_updated": ""},     # poll 1: still running, field now present but empty
      {"mirror_updated": "t1"},   # poll 2: converged — real, non-empty value
    ])
    def fake_api(method, path, *args, **kwargs):
      if method == "POST":
        return 200, {}
      return 200, next(get_responses)
    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      result = mf.ensure_synced(SRC)
    assert result is True
    assert mf._is_fresh(SRC.key()) is True

  def test_pre_snapshot_api_failure_returns_false_immediately_no_blind_poll(self):
    """If the pre-sync mirror_updated snapshot itself fails (API error/exception), there is
    no baseline to detect convergence against — ensure_synced must fail open immediately
    rather than blindly polling. mirror-sync POST must not be issued in this case."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30)
    with patch.object(mf, "_get_mirror_updated", return_value=(False, None)) as mock_get, \
         patch("proxy.forgejo_api") as mock_api, \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_get.assert_called_once()
    mock_api.assert_not_called()
    mock_log_error.assert_called()

  def test_transient_api_failure_during_poll_does_not_falsely_converge(self):
    """A transient API failure mid-poll (ok=False) must not be mistaken for convergence —
    the loop must keep polling (and eventually converge once a real value appears)."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=30)
    with patch.object(mf, "_get_mirror_updated", side_effect=[
           (True, "t0"),    # prev
           (False, None),   # poll 1: transient API error — must not count as convergence
           (True, "t1"),    # poll 2: real convergence
         ]) as mock_get, \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch("proxy.time.sleep"):
      result = mf.ensure_synced(SRC)
    assert result is True
    assert mock_get.call_count == 3

  # -------------------------------------------------------------------------
  # BUG 2 regression: unexpected exception in the leader path must not escape
  # ensure_synced, and must not leave single-flight bookkeeping stuck.
  # -------------------------------------------------------------------------

  def test_unexpected_exception_in_leader_path_is_caught_fail_open(self):
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=30)
    with patch.object(mf, "_get_mirror_updated", side_effect=[(True, "t0"), (True, "t1")]), \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch.object(mf, "_mark_fresh", side_effect=RuntimeError("boom")), \
         patch("proxy.time.sleep"), \
         patch.object(proxy.log, "error") as mock_log_error:
      result = mf.ensure_synced(SRC)
    assert result is False
    mock_log_error.assert_called()
    # Bookkeeping must be cleared despite the exception, so a subsequent call can proceed.
    assert mf._ensure_inflight == {}
    with patch.object(mf, "_get_mirror_updated", side_effect=[(True, "t0"), (True, "t1")]), \
         patch("proxy.forgejo_api", return_value=(200, {})), \
         patch("proxy.time.sleep"):
      assert mf.ensure_synced(SRC) is True

  # -------------------------------------------------------------------------
  # BUG 3 regression: the freshness cache must not grow unbounded.
  # -------------------------------------------------------------------------

  def test_freshness_cache_is_capped(self):
    """Cache capping is a property of _mark_fresh itself, independent of wait/async — exercised
    here via wait mode (synchronous, deterministic) since async would require waiting for 25
    background threads to individually converge."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=3600, cache_max=10, sync_wait_timeout=5)
    call_n = {"n": 0}

    def fake_api(method, path, *args, **kwargs):
      if method == "POST":
        return 200, {}
      call_n["n"] += 1
      return 200, {"mirror_updated": f"t{call_n['n']}"}  # always a fresh, distinct value

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      for i in range(25):
        assert mf.ensure_synced(SourceRepo("github.com", f"owner{i}/repo")) is True
    assert len(mf._last_sync) <= 10

  def test_single_flight_concurrent_real_threads_wait_mode_one_sync_call(self):
    """Two real threads calling ensure_synced for the same repo concurrently, in wait
    mode, must coalesce into exactly one mirror-sync POST — the follower joins the
    leader's in-flight Event rather than triggering its own sync."""
    mf = self._freshness(sync_mode="wait", freshness_ttl=30, sync_wait_timeout=5)
    entered = threading.Barrier(2, timeout=5)
    release = threading.Event()
    post_count = {"n": 0}
    lock = threading.Lock()

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        return 200, {"mirror_updated": "t0" if not post_count["n"] else "t1"}
      with lock:
        post_count["n"] += 1
      release.wait(timeout=5)
      return 200, {}

    results = [None, None]

    def leader_worker():
      entered.wait()
      results[0] = mf.ensure_synced(SRC)

    def follower_worker():
      entered.wait()
      results[1] = mf.ensure_synced(SRC)

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      t1 = threading.Thread(target=leader_worker)
      t2 = threading.Thread(target=follower_worker)
      t1.start()
      t2.start()
      time.sleep(0.2)
      release.set()
      t1.join(timeout=5)
      t2.join(timeout=5)

    assert not t1.is_alive() and not t2.is_alive()
    assert post_count["n"] == 1
    assert results[0] is True
    assert results[1] is True

  def test_single_flight_async_mode_follower_returns_true_without_waiting(self):
    """In async mode, the leader itself returns immediately after spawning a background
    thread to do the real sync-and-wait. A follower arriving while that background sync is
    still in flight must also return True immediately, without waiting for it to finish, and
    must not trigger a second POST /mirror-sync (single-flight dedup)."""
    mf = self._freshness(sync_mode="async", freshness_ttl=30, sync_wait_timeout=5)
    post_started = threading.Event()
    release = threading.Event()
    post_count = {"n": 0}
    lock = threading.Lock()

    def fake_api(method, path, *args, **kwargs):
      if method == "GET":
        # Pre-snapshot (before POST) is "t0"; once the POST has been released, subsequent
        # polls report a new value so the background thread converges promptly instead of
        # spinning to its own sync_wait_timeout.
        return 200, {"mirror_updated": "t1" if release.is_set() else "t0"}
      with lock:
        post_count["n"] += 1
      post_started.set()
      release.wait(timeout=5)
      return 200, {}

    with patch("proxy.forgejo_api", side_effect=fake_api), \
         patch("proxy.time.sleep"):
      # Leader call: spawns the background thread and returns immediately (does not block
      # on fake_api itself — the background thread does).
      leader_result = mf.ensure_synced(SRC)
      assert leader_result is True

      # Wait for the background thread to actually reach the POST call, proving the
      # background sync is genuinely in flight before the follower shows up.
      assert post_started.wait(timeout=5)

      # Follower call: sync still in flight (background thread blocked in fake_api's POST).
      # Must return True immediately without waiting and without a second POST.
      follower_result = mf.ensure_synced(SRC)
      assert follower_result is True
      assert post_count["n"] == 1, "follower must not trigger a second mirror-sync POST"

      release.set()
      deadline = time.monotonic() + 5
      while mf._ensure_inflight and time.monotonic() < deadline:
        time.sleep(0.01)

    assert mf._ensure_inflight == {}
    assert post_count["n"] == 1


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
      first = mf.trigger_sync(SRC)
      second = mf.trigger_sync(SRC)

    assert first is True
    assert second is False

  def test_flag_cleared_after_do_sync_completes_allows_retrigger(self):
    mf = self._freshness()
    key = SRC.key()
    done = threading.Event()
    real_do_sync = mf._do_sync

    def instrumented_do_sync(source_repo, k):
      try:
        real_do_sync(source_repo, k)
      finally:
        done.set()

    with patch.object(mf, "_do_sync", side_effect=instrumented_do_sync), \
         patch("proxy.forgejo_api", return_value=(200, {})):
      assert mf.trigger_sync(SRC) is True
      assert done.wait(timeout=2), "background sync thread did not complete in time"
      assert key not in mf._sync_triggered
      assert mf.trigger_sync(SRC) is True


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
      path="/github.com/owner/repo/git-upload-pack",
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
      handler.proxy_to_forgejo(SRC, "/git-upload-pack")

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
      path="/github.com/owner/repo/git-upload-pack",
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
      handler.proxy_to_forgejo(SRC, "/git-upload-pack")

    req = captured["req"]
    assert req.data == body
    assert req.headers.get("Content-length") == str(len(body))

  def test_malformed_chunk_size_returns_400_no_upstream(self):
    handler = _make_handler(
      path="/github.com/owner/repo/git-upload-pack",
      command="POST",
      headers={"Transfer-Encoding": "chunked"},
      rfile_bytes=b"zz\r\nhello\r\n0\r\n\r\n",  # "zz" is not a hex chunk size
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    with patch("proxy.urllib.request.urlopen") as urlopen:
      handler.proxy_to_forgejo(SRC, "/git-upload-pack")
    handler.send_response.assert_called_once_with(400)
    urlopen.assert_not_called()

  def test_oversized_chunked_body_returns_400(self):
    # A declared chunk larger than the cap must abort (before reading the data) with 400.
    huge = format(proxy.REQUEST_BODY_MAX + 1, "x").encode()
    handler = _make_handler(
      path="/github.com/owner/repo/git-upload-pack",
      command="POST",
      headers={"Transfer-Encoding": "chunked"},
      rfile_bytes=huge + b"\r\n",  # size line only; parsing aborts before reading the body
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    with patch("proxy.urllib.request.urlopen") as urlopen:
      handler.proxy_to_forgejo(SRC, "/git-upload-pack")
    handler.send_response.assert_called_once_with(400)
    urlopen.assert_not_called()

  def test_non_integer_content_length_returns_400(self):
    handler = _make_handler(
      path="/github.com/owner/repo/git-upload-pack",
      command="POST",
      headers={"Content-Length": "not-a-number"},
      rfile_bytes=b"",
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()
    with patch("proxy.urllib.request.urlopen") as urlopen:
      handler.proxy_to_forgejo(SRC, "/git-upload-pack")
    handler.send_response.assert_called_once_with(400)
    urlopen.assert_not_called()

  def test_forgejo_path_reconstructed_with_mirror_name_and_query(self):
    """proxy_to_forgejo must rebuild the path as /<user>/<mirror_name><service>?<query>,
    surviving a nested repo path and a query string (info/refs)."""
    handler = _make_handler(
      path="/gitlab.com/group/sub/repo/info/refs?service=git-upload-pack",
      command="GET",
      headers={},
      rfile_bytes=b"",
    )
    handler.send_response = MagicMock()
    handler.send_header = MagicMock()
    handler.end_headers = MagicMock()

    captured = {}

    def fake_urlopen(req, timeout=None):
      captured["req"] = req
      return _fake_forgejo_response()

    src = SourceRepo("gitlab.com", "group/sub/repo")
    with patch("proxy.urllib.request.urlopen", side_effect=fake_urlopen):
      handler.proxy_to_forgejo(src, "/info/refs")

    url = captured["req"].full_url
    assert url.endswith(f"/{proxy.FORGEJO_USER}/{src.mirror_name()}/info/refs?service=git-upload-pack")


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
    h.send_header = MagicMock()
    h.end_headers = MagicMock()
    h.wfile = io.BytesIO()
    h.wait_for_mirror = MagicMock()
    h.proxy_to_forgejo = MagicMock()
    h.send_git_error = MagicMock()
    return h

  def test_mirror_missing_creates_and_waits_then_errors_if_still_empty(self):
    handler = self._handler("/github.com/owner/repo/info/refs?service=git-upload-pack")
    handler.wait_for_mirror.return_value = None
    with patch.object(proxy, "get_mirror", return_value=None), \
         patch.object(proxy, "create_mirror") as mock_create:
      handler.handle_git_request()
    mock_create.assert_called_once_with(SRC)
    handler.wait_for_mirror.assert_called_once_with(SRC)
    handler.send_git_error.assert_called_once()
    handler.proxy_to_forgejo.assert_not_called()

  def test_mirror_empty_triggers_sync_then_waits(self):
    handler = self._handler("/github.com/owner/repo/info/refs?service=git-upload-pack")
    handler.wait_for_mirror.return_value = {"empty": False}
    with patch.object(proxy, "get_mirror", return_value={"empty": True}), \
         patch.object(proxy, "create_mirror") as mock_create:
      handler.handle_git_request()
    handler.server.freshness.trigger_sync.assert_called_once_with(SRC)
    mock_create.assert_not_called()
    handler.wait_for_mirror.assert_called_once_with(SRC)
    handler.proxy_to_forgejo.assert_called_once_with(SRC, "/info/refs")

  def test_populated_mirror_info_refs_upload_pack_calls_ensure_synced(self):
    handler = self._handler(
      "/github.com/owner/repo/info/refs?service=git-upload-pack",
      command="GET",
    )
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_synced.assert_called_once_with(SRC)
    handler.proxy_to_forgejo.assert_called_once_with(SRC, "/info/refs")

  def test_nested_gitlab_path_parsed_and_proxied(self):
    handler = self._handler(
      "/gitlab.com/group/sub/repo/info/refs?service=git-upload-pack",
      command="GET",
    )
    src = SourceRepo("gitlab.com", "group/sub/repo")
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_synced.assert_called_once_with(src)
    handler.proxy_to_forgejo.assert_called_once_with(src, "/info/refs")

  def test_populated_mirror_post_git_upload_pack_calls_ensure_synced(self):
    """POST .../git-upload-pack is where the client sends `want <sha>` lines, including
    arbitrary commit SHAs — this must also trigger ensure_synced."""
    handler = self._handler("/github.com/owner/repo/git-upload-pack", command="POST")
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_synced.assert_called_once_with(SRC)
    handler.proxy_to_forgejo.assert_called_once_with(SRC, "/git-upload-pack")

  def test_populated_mirror_get_info_refs_wrong_service_skips_ensure_synced(self):
    handler = self._handler(
      "/github.com/owner/repo/info/refs?service=git-receive-pack",
      command="GET",
    )
    with patch.object(proxy, "get_mirror", return_value={"empty": False}):
      handler.handle_git_request()
    handler.server.freshness.ensure_synced.assert_not_called()
    handler.proxy_to_forgejo.assert_called_once_with(SRC, "/info/refs")

  def test_post_receive_pack_rejected_403_read_only(self):
    """A push (POST /git-receive-pack) must be rejected with 403 — mirrors are read-only —
    and must not create a mirror or proxy anything upstream."""
    handler = self._handler("/github.com/owner/repo/git-receive-pack", command="POST")
    with patch.object(proxy, "get_mirror") as mock_get_mirror, \
         patch.object(proxy, "create_mirror") as mock_create:
      handler.handle_git_request()
    handler.send_response.assert_called_once_with(403)
    mock_get_mirror.assert_not_called()
    mock_create.assert_not_called()
    handler.proxy_to_forgejo.assert_not_called()

  def test_invalid_segment_rejected_with_400_no_get_mirror(self):
    handler = self._handler("/github.com/a b/x/info/refs?service=git-upload-pack")
    with patch.object(proxy, "get_mirror") as mock_get_mirror:
      handler.handle_git_request()
    mock_get_mirror.assert_not_called()
    handler.send_response.assert_called_once_with(400)
    handler.end_headers.assert_called_once()
    handler.proxy_to_forgejo.assert_not_called()

  def test_non_allowlisted_host_rejected_with_400_no_get_mirror(self):
    handler = self._handler("/bitbucket.org/owner/repo/info/refs?service=git-upload-pack")
    with patch.object(proxy, "get_mirror") as mock_get_mirror:
      handler.handle_git_request()
    mock_get_mirror.assert_not_called()
    handler.send_response.assert_called_once_with(400)
    handler.end_headers.assert_called_once()
    handler.proxy_to_forgejo.assert_not_called()
