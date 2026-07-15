import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))

import pytest

import source


@pytest.fixture(autouse=True)
def _source_registry():
  """Pin a known source allowlist (github.com + gitlab.com) for every test, so path
  parsing, config validation and hook matching exercise nested gitlab paths. Modules
  read `source.REGISTRY` dynamically, so overriding it here reaches all of them."""
  saved = source.REGISTRY
  source.REGISTRY = source.SourceRegistry.from_env("github.com,gitlab.com")
  yield
  source.REGISTRY = saved


class NoOpThread:
  """Stand-in for threading.Thread that never actually runs the target,
  so any "in-flight" flag guarded by the real background thread stays set
  for the duration of the test. Used to test dedup/guard logic without
  racing a real thread."""

  def __init__(self, target=None, args=(), kwargs=None, daemon=None):
    pass

  def start(self):
    pass
