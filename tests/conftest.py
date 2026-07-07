import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "proxy"))


class NoOpThread:
  """Stand-in for threading.Thread that never actually runs the target,
  so any "in-flight" flag guarded by the real background thread stays set
  for the duration of the test. Used to test dedup/guard logic without
  racing a real thread."""

  def __init__(self, target=None, args=(), kwargs=None, daemon=None):
    pass

  def start(self):
    pass
