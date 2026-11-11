"""Test bootstrap for toolrecall-light.

The tests import the plugin's top-level modules (``dedup``, ``store``, ``schemas``)
and the package itself as ``__init__``, so the plugin root must be on ``sys.path``
regardless of which directory pytest is invoked from or which rootdir it picks.
Without this, ``pytest <plugin-root>/tests`` from a parent directory fails at
collection with ``ModuleNotFoundError: No module named 'dedup'``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = str(Path(__file__).resolve().parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
