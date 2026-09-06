"""One home for "root ignores the mode bits this test sets".

Four test files set a mode and assert the resulting ``OSError``, and
each carried its own copy of the skip. Two of those copies landed in one
PR (#352) and the third was already there, so the drift was visible in a
single diff: ``tests/test_appendio.py`` set ``0o200`` and asserted the
raise with no skip at all, which fails outright in a container CI that
runs as root.

The predicate is ``os.geteuid`` guarded by ``hasattr``, because Windows
has no such call and the marker is evaluated at import time on every
platform.
"""

from __future__ import annotations

import os

import pytest

#: Skip a test whose whole subject is a permission the superuser does
#: not have to obey.
skip_as_root = pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores the mode bits this test sets",
)
