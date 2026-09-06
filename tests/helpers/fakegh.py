"""Fake `gh` binaries and payload rows, shared by the R10.7 suites.

Two files exercise the open-PR bound: `tests/test_open_pr_counter.py`
(what a payload means) and `tests/test_flow_control.py` (what the daemon
does about it). Both need the same fake `gh`, and a second copy of it
would be a second thing to keep true.

PATH rather than a patched `subprocess.run` wherever the lookup, the
process and the decode are part of what is being asserted.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from kstrl.pr import PR_FOOTER_MARKER
from tests.test_serve_seam import _write_executable

#: Emits whatever JSON the test put in FAKE_GH_JSON. The real
#: `count_open_kstrl_prs` runs against it, subprocess and json.loads
#: included, so a change to the argv or the parse is caught.
FAKE_GH = """#!/bin/sh
cat "$FAKE_GH_JSON"
"""

#: Records that it ran, then fails. A test that asserts GitHub was NOT
#: reached needs the fake to be observable when it IS reached, and an
#: exit code the counter cannot mistake for a count.
FAKE_GH_MARKER = """#!/bin/sh
touch "$FAKE_GH_MARKER_PATH"
exit 99
"""

#: Counts its own invocations in FAKE_GH_COUNT and succeeds on the
#: third, so one loop can be driven through fail, fail, succeed, fail,
#: fail. A streak that survives a good count is only observable inside
#: ONE `serve` call, because `serve` builds its own streak per call.
FAKE_GH_THIRD_CALL_WORKS = """#!/bin/sh
n=$(cat "$FAKE_GH_COUNT" 2>/dev/null || echo 0)
n=$((n + 1))
echo "$n" > "$FAKE_GH_COUNT"
if [ "$n" -eq 3 ]; then
  cat "$FAKE_GH_JSON"
else
  exit 99
fi
"""

#: Where `gh` is actually spawned. `count_open_kstrl_prs` goes through
#: `intake_github.run_gh`, so patching `kstrl.serve.subprocess.run` would
#: patch nothing and the tests would silently reach the real `gh`.
GH_RUN = "kstrl.intake_github.subprocess.run"


def put_gh_on_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> Path:
    """Make ``body`` the `gh` that a PATH lookup finds; return its path."""
    bindir = tmp_path / "fakebin"
    bindir.mkdir(exist_ok=True)
    path = _write_executable(bindir / "gh", body)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return path


def install_fake_gh(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    rows: list[dict[str, object]],
) -> Path:
    """A `gh` that prints ``rows``; returns the JSON file it reads.

    Rewrite the returned file to change what the next call sees.
    """
    put_gh_on_path(tmp_path, monkeypatch, FAKE_GH)
    payload = tmp_path / "fake_gh.json"
    payload.write_text(json.dumps(rows), encoding="utf-8")
    monkeypatch.setenv("FAKE_GH_JSON", str(payload))
    return payload


def install_marker_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `gh` that touches a marker and exits 99; returns the marker."""
    put_gh_on_path(tmp_path, monkeypatch, FAKE_GH_MARKER)
    marker = tmp_path / "gh_was_called"
    monkeypatch.setenv("FAKE_GH_MARKER_PATH", str(marker))
    return marker


def completed(returncode: int, *, stdout: str = "", stderr: str = "") -> CompletedProcess[str]:
    return CompletedProcess(args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr)


def marked(number: int) -> dict[str, object]:
    """A PR row whose body ends with the footer, as kstrl writes it."""
    return {"number": number, "body": f"Some body\n\n---\n{PR_FOOTER_MARKER}"}


def unmarked(number: int) -> dict[str, object]:
    return {"number": number, "body": "A hand-written PR body"}
