"""R10.7: what a `gh pr list` payload means, and what counts as a kstrl PR.

`count_open_kstrl_prs` turns one gh invocation into a number the daemon
gates on, so every way that number can be wrong is a way the bound
silently switches off. Two whole classes of that are covered here: a
payload the counter cannot read (which must refuse, never count as
zero), and a body that merely mentions the footer (which must not
count). `tests/test_flow_control.py` holds what the daemon does with the
answer.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

import kstrl.pr
from kstrl.pr import GH_TIMEOUT, PR_FOOTER_MARKER, _generate_pr_body
from kstrl.serve import OpenPrCount, count_open_kstrl_prs
from tests.helpers.astwalk import assert_census, folds_to, package_sources
from tests.helpers.fakegh import GH_RUN as _GH_RUN
from tests.helpers.fakegh import completed as _completed
from tests.helpers.fakegh import install_fake_gh as _install_fake_gh
from tests.helpers.fakegh import marked as _marked
from tests.helpers.fakegh import unmarked as _unmarked
from tests.test_pr import _test_manifest

# ---------------------------------------------------------------------------
# The counter
# ---------------------------------------------------------------------------


class TestCountOpenKstrlPrs:
    def test_filters_by_marker(self, tmp_path: Path) -> None:
        rows = [_marked(1), _unmarked(2), _marked(3)]

        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))) as run:
            assert count_open_kstrl_prs(tmp_path) == OpenPrCount(count=2, saturated=False)

        assert run.call_args.args[0] == [
            "gh",
            "pr",
            "list",
            "--state",
            "open",
            "--limit",
            "100",
            "--json",
            "number,body",
        ]
        assert run.call_args.kwargs["timeout"] == GH_TIMEOUT
        assert run.call_args.kwargs["cwd"] == str(tmp_path)

    def test_limit_reaches_the_argv(self, tmp_path: Path) -> None:
        """Position, not membership: `"7"` must be `--limit`'s value."""
        with patch(_GH_RUN, return_value=_completed(0, stdout="[]")) as run:
            assert count_open_kstrl_prs(tmp_path, limit=7).count == 0
        argv = run.call_args.args[0]
        assert argv[argv.index("--limit") + 1] == "7"

    def test_a_full_page_is_reported_as_saturated(self, tmp_path: Path) -> None:
        """`len(rows) >= limit` is the only evidence gh gives of truncation.

        There is no "hasNextPage" in this output, so the count that comes
        back is a lower bound whenever the page is full and the caller
        has to be told.
        """
        rows = [_unmarked(n) for n in range(5)]
        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))):
            counted = count_open_kstrl_prs(tmp_path, limit=5)
        assert counted == OpenPrCount(count=0, saturated=True)

        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))):
            counted = count_open_kstrl_prs(tmp_path, limit=6)
        assert counted == OpenPrCount(count=0, saturated=False)

    @pytest.mark.parametrize(
        "payload",
        [
            ["a", "b"],
            [1, 2, 3],
            [{"number": 1}],
            [[_marked(1)]],
            [None],
            [_marked(1), {"number": 2}],
        ],
        ids=["strings", "ints", "no body key", "nested list", "null row", "one good one bad"],
    )
    def test_a_row_that_is_not_a_pr_record_refuses(
        self,
        tmp_path: Path,
        payload: list[object],
    ) -> None:
        """Validate the RAW payload entry by entry, with the row's index.

        Each of these six shapes counted as ZERO before, and the gate
        then ADMITTED on "0 of 1 kstrl PRs open" with the true number
        unknown. The `isinstance(row, dict)` clause that produced it read
        as defensive and was the fail-open: without it a bad row raises,
        with it the row is silently discarded.
        """
        with (
            patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(payload))),
            pytest.raises(RuntimeError, match="is not a PR record"),
        ):
            count_open_kstrl_prs(tmp_path)

    def test_the_refusal_names_the_row_index(self, tmp_path: Path) -> None:
        payload = [_marked(1), _marked(2), {"number": 3}]
        with (
            patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(payload))),
            pytest.raises(RuntimeError, match=r"row 2 is not a PR record"),
        ):
            count_open_kstrl_prs(tmp_path)

    def test_a_null_body_is_a_record_and_counts_as_unmarked(self, tmp_path: Path) -> None:
        """gh returns `"body": null` for an empty description.

        That IS a PR record, so it must not be refused; it simply does
        not end with the marker.
        """
        payload = [{"number": 1, "body": None}, _marked(2)]
        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(payload))):
            assert count_open_kstrl_prs(tmp_path).count == 1

    @pytest.mark.parametrize(
        ("patch_kwargs", "match"),
        [
            ({"return_value": _completed(1, stderr="gh: auth required\n")}, "auth required"),
            (
                {"side_effect": subprocess.TimeoutExpired(cmd=["gh"], timeout=GH_TIMEOUT)},
                "timed out",
            ),
            ({"side_effect": FileNotFoundError("gh")}, "could not run"),
            ({"return_value": _completed(0, stdout="not json")}, "unparseable"),
            ({"return_value": _completed(0, stdout='{"number": 1}')}, "expected a list"),
        ],
        ids=["gh error", "timeout", "exec failed", "unparseable", "non-list payload"],
    )
    def test_every_failure_shape_raises(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        patch_kwargs: dict[str, object],
        match: str,
    ) -> None:
        """Each failure the counter can meet becomes a RuntimeError.

        `shutil.which` is pinned so these say the same thing on a machine
        with no `gh`; the missing-binary case is its own test below.
        """
        monkeypatch.setattr("shutil.which", lambda _: "/usr/bin/gh")
        with patch(_GH_RUN, **patch_kwargs), pytest.raises(RuntimeError, match=match):
            count_open_kstrl_prs(tmp_path)

    def test_raises_when_gh_is_not_installed(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("shutil.which", lambda _: None)
        with pytest.raises(RuntimeError, match="not installed"):
            count_open_kstrl_prs(tmp_path)

    def test_counts_through_a_real_subprocess(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """End to end: PATH lookup, process, stdout, decode, filter."""
        _install_fake_gh(tmp_path, monkeypatch, [_marked(1), _unmarked(2)])
        assert count_open_kstrl_prs(tmp_path).count == 1


# ---------------------------------------------------------------------------
# What "carries the marker" means
# ---------------------------------------------------------------------------


class TestMarkerIsAnchoredAtTheEnd:
    """The match is `endswith`, not `in`, and that is load-bearing.

    A substring match made any pull request that MENTIONS the footer
    count as one kstrl opened. Measured live against 0xfauzi/kstrl while
    this was under review: 8 open PRs, exactly one matched, and it was
    PR #354 itself, because its own Summary quoted the constant. The
    daemon can wedge itself on prose that way, with `max_open_prs = 0`
    as the only exit.
    """

    def test_a_body_ending_with_the_marker_counts(self, tmp_path: Path) -> None:
        rows = [{"number": 1, "body": f"Summary\n\n---\n{PR_FOOTER_MARKER}"}]
        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))):
            assert count_open_kstrl_prs(tmp_path).count == 1

    def test_trailing_whitespace_does_not_break_the_anchor(self, tmp_path: Path) -> None:
        """GitHub returns bodies with trailing newlines; `rstrip` first."""
        rows = [{"number": 1, "body": f"Summary\n\n---\n{PR_FOOTER_MARKER}\r\n\n  "}]
        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))):
            assert count_open_kstrl_prs(tmp_path).count == 1

    def test_a_body_that_only_mentions_the_marker_does_not_count(self, tmp_path: Path) -> None:
        rows = [
            {
                "number": 1,
                "body": (
                    f"This PR changes the footer `{PR_FOOTER_MARKER}` that the "
                    "open-PR bound counts.\n\nSee the diff for the constant."
                ),
            }
        ]
        with patch(_GH_RUN, return_value=_completed(0, stdout=json.dumps(rows))):
            assert count_open_kstrl_prs(tmp_path).count == 0

    def test_the_real_writer_ends_its_body_with_the_marker(self) -> None:
        """The anchor is only free if kstrl really writes it last.

        Asserted through `_generate_pr_body`, the production writer, not
        a test fixture imitating it: no network, no gh, no manifest on
        disk. If a future edit appends anything after the footer, this
        test goes red rather than the bound quietly counting zero.
        """
        manifest = _test_manifest()
        body = _generate_pr_body(manifest.components[0], manifest)
        assert body.rstrip().endswith(PR_FOOTER_MARKER)

    def test_the_other_writer_joins_immediately_after_the_footer(self) -> None:
        """`create_single_pr` pushes a branch, so it is read statically.

        Both marker appends in `kstrl/pr.py` must be the LAST line added
        before the join that produces the body. A source check rather
        than a behavioural one because the only way to reach that writer
        is a real `git push`.
        """
        lines = Path(kstrl.pr.__file__).read_text(encoding="utf-8").splitlines()
        followers = [
            next(follower for follower in lines[index + 1 :] if follower.strip())
            for index, line in enumerate(lines)
            if line.strip() == "lines.append(PR_FOOTER_MARKER)"
        ]
        assert len(followers) == 2, "expected two footer sites in kstrl/pr.py"
        assert all('"\\n".join(lines)' in follower for follower in followers), (
            "A kstrl PR body must END with PR_FOOTER_MARKER: the open-PR "
            "bound matches the footer with endswith, so anything appended "
            f"after it makes every PR kstrl opens uncountable. Saw: {followers}"
        )


# ---------------------------------------------------------------------------
# The marker constant
# ---------------------------------------------------------------------------


#: The marker is spelled ONCE in ``kstrl/``: the constant's own
#: definition. Anything else is a second spelling, which is the drift the
#: hoist exists to prevent - a reader in another module reaches for the
#: nearest spelling, and a footer reword then makes the bound count zero
#: while every test stays green.
EXPECTED_MARKER_SPELLINGS: dict[str, int] = {"pr.py": 1}


class TestFooterMarker:
    def test_the_marker_is_spelled_once_in_the_package(self) -> None:
        """Layer 1, the net: every expression in ``kstrl/`` that folds to
        the marker, counted per module, whatever it does with the string
        afterwards. Package-wide rather than scoped to ``pr.py``, because
        the modules that will grow a second spelling are the READERS -
        this bound, the dampener, the polled steering channel (#231) -
        and a guard that only reads ``pr.py`` cannot see them."""
        assert_census(
            sources=package_sources(),
            sees=folds_to(PR_FOOTER_MARKER),
            expected=EXPECTED_MARKER_SPELLINGS,
            control=f'footer = "{PR_FOOTER_MARKER}"\n',
            message=(
                "The set of places spelling the kstrl PR footer changed. A "
                "reader identifying a kstrl-authored PR must import "
                "PR_FOOTER_MARKER from kstrl.pr, not repeat the literal: the "
                "open-PR bound counts bodies containing it, so a second "
                "spelling that drifts makes the count silently zero."
            ),
        )

    def test_the_marker_literal_is_pinned(self) -> None:
        """Layer 3, the value. The census folds AGAINST the constant, so
        it moves with any reword and stays green; `tests/test_pr.py`
        asserts `"kstrl" in body`, which survives one too. A measured
        mutation changing the URL to `kstrl-loop` left all 308 tests in
        the three serve suites green.

        Rewording this line silently un-counts every pull request open at
        the moment of the reword - the same class as the ralph rename,
        which already did it once. Changing the constant is allowed;
        changing it without reading this is not."""
        assert PR_FOOTER_MARKER == "Generated by [kstrl](https://github.com/0xfauzi/kstrl)"

    def test_both_footer_sites_use_the_constant(self) -> None:
        """Layer 2, the message: ``pr.py``'s two writers still go through
        the constant. The census above cannot say this - deleting a
        footer site leaves the spelling count at 1 - and "you wrote the
        footer without the constant" is the wrong message for it."""
        source = Path(kstrl.pr.__file__).read_text(encoding="utf-8")
        assert source.count("lines.append(PR_FOOTER_MARKER)") == 2
