"""Positive controls for the append-open guard: source it MUST flag.

The guard lives in ``tests/test_append_opens_have_one_home.py`` and its
assertions pin an inventory or assert a list is empty. Both are also
what a switched-off detector returns, so without this file layer 1's
predicate could be replaced by ``return False`` and layer 3's callee
resolution deleted, and that file would notice only through its
inventories. #324 records eleven instances of a guard reporting clean
because it stopped looking, every one in that direction.

Separate from the guard for the reason
``tests/test_journal_guard_detects.py`` gives for the same split: the
800-line ratchet fired, and the seam is the right one. That file walks
the package and pins what is there; this one feeds the predicates
snippets and pins what they say. Importing the walkers across test
modules is the house pattern here.

The three disclosed limits live here too, each under a strict xfail, so
a later widening XPASSes and fails loudly rather than agreeing silently
(CLAUDE.md rule 2: ``assert hits == []`` is not a control).
"""

from __future__ import annotations

import pytest

from tests.helpers.astwalk import blind_spot
from tests.test_append_opens_have_one_home import append_opens_in, routed_calls_in


class TestTheGuardDetects:
    """Each net fired at source it is supposed to see, and stayed quiet at
    source it is not. A guard whose only assertion is that an inventory
    matches cannot notice its own detector being switched off, and #324
    records eleven instances of exactly that, every one in the direction
    of going blind.

    Layer 1's rows come first, then layer 3's. Both nets pool alias
    names from the SNIPPET here rather than from ``kstrl/``, which is
    what makes an alias row testable at all: the census predicates build
    their names from the package, so a snippet's own alias could never
    be in that set."""

    @pytest.mark.parametrize(
        "source",
        [
            'open(p, "a")\n',
            'open(p, mode="a")\n',
            'open(p, "ab")\n',
            'open(p, "a+b")\n',
            'p.open("a")\n',
            'p.open(mode="a")\n',
            "os.open(p, flags)\n",
            'os.fdopen(fd, "a")\n',
            'tempfile.NamedTemporaryFile(mode="a")\n',
            '_o = open\n_o(p, "a")\n',
        ],
    )
    def test_an_append_open_is_seen(self, source: str) -> None:
        assert append_opens_in(source), f"the walk missed an append open: {source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            'open(p, "r")\n',
            'open(p, "w")\n',
            'open(p, "rb")\n',
            "open(p)\n",
            'p.write_text("x")\n',
        ],
    )
    def test_a_read_or_a_truncating_write_is_not_seen(self, source: str) -> None:
        assert not append_opens_in(source), f"the walk over-matched: {source!r}"

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_an_append_reached_by_seeking_to_the_end_is_missed(self) -> None:
        """The disclosed limit, pinned so the disclosure cannot rot.

        ``"r+"`` opens for update without truncating, and a seek to the
        end turns it into an append. The mode contains no ``"a"``, so
        layer 1 does not count it and layer 2 never sees it. It is not
        merely unpinned: nothing in this suite would notice such a
        writer arriving.

        Not fixed by widening the mode test to ``"+"``: every ``"a+"``
        lock file above would stay counted while every ``"r+"`` reader
        that never seeks would join them, which trades a disclosed miss
        for undisclosed noise. The day somebody does widen it, this row
        XPASSes and ``strict=True`` makes that a failure, so the
        disclosure is edited in the same diff.
        """
        blind_spot(append_opens_in, 'h = open(p, "r+")\nh.seek(0, 2)\nh.write(line)\n')

    @pytest.mark.parametrize(
        "source",
        [
            'append_records(p, line, repair="")\n',
            "appendio.append_records(p, line, repair=r)\n",
            "open_for_append(p)\n",
            "with appending(p) as h:\n    h.write(line)\n",
            'from kstrl.appendio import append_records as _ar\n_ar(p, line, repair="")\n',
            "from kstrl.appendio import appending as _ap\nwith _ap(p) as h:\n    h.write(line)\n",
            '_w = append_records\n_w(p, line, repair="")\n',
            '_w = append_records\n_v = _w\n_v(p, line, repair="")\n',
        ],
    )
    def test_a_call_into_appendio_is_seen(self, source: str) -> None:
        """Layer 3's detect set, and the last four are #352 round 2.

        Measured against the predicate before the fix: the two bare and
        attribute forms were seen and every aliased one was not, so a new
        appender written that way got no exclusion and changed no pinned
        inventory. The two-hop row is the fixed point: one hop closes
        ``_w = append_records`` and leaves ``_v = _w`` open.
        """
        assert routed_calls_in(source), f"layer 3 missed a routed append: {source!r}"

    @pytest.mark.parametrize(
        "source",
        [
            'open(p, "a")\n',
            "append_terminated(handle, line, repair=r)\n",
            "_w = append_records\n",
            "records.append(line)\n",
        ],
    )
    def test_something_that_is_not_a_call_into_appendio_is_not_seen(self, source: str) -> None:
        """The other direction. ``append_terminated`` takes a handle
        somebody else obtained here, so counting it would count the same
        site twice, and a bare rebind with no call is not a site."""
        assert not routed_calls_in(source), f"layer 3 over-matched: {source!r}"

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_an_entry_point_stored_on_an_attribute_is_missed(self) -> None:
        """Layer 3's first disclosed limit, pinned so it cannot rot.

        ``bound_names`` drops a dotted target on purpose, so
        ``self._append = append_records`` binds nothing this walk can
        follow and ``self._append(...)`` has leaf name ``_append``.
        Not fixed by taking dotted targets: the alias table is over LOCAL
        names, an attribute is per instance rather than per module, and
        the name ``_append`` would then count in every other module too,
        which is over-matching a flagging guard can afford only while the
        names are rare. The day somebody widens it this XPASSes and
        ``strict=True`` makes that a failure.
        """
        blind_spot(
            routed_calls_in,
            "self._append = append_records\nself._append(p, line, repair=r)\n",
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_an_entry_point_reached_through_a_partial_is_missed(self) -> None:
        """Layer 3's second disclosed limit, and the likelier of the two.

        ``_locked = partial(append_records, lock=True)`` binds a Call
        rather than a Name, so the fixed point does not follow it, and
        the ``lock`` the census exists to pin is on the partial rather
        than on the call. The same is true of any callable held in a
        container or returned from a factory. Stated rather than fixed:
        following a value through an arbitrary expression is a resolver
        this suite does not have, and ``assert routed_calls_in(...) == []``
        would be no control at all (CLAUDE.md rule 2).
        """
        blind_spot(
            routed_calls_in,
            "_locked = partial(append_records, lock=True)\n_locked(p, line, repair=r)\n",
        )
