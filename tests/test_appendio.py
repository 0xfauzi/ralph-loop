"""The contract ``kstrl/appendio.py`` offers its six callers.

Separate from ``tests/test_journal_torn_tail.py`` and
``tests/test_append_torn_tail.py`` because the subject is different.
Those two measure what a tear COSTS one particular file, in records its
own production reader returns. This one is the helper's own promises,
with no journal, no progress log and no reader in it: that a payload
without a terminator is refused rather than quietly fixed, that an empty
payload is not an excuse to write a repair row, and that a caller who
asks for a bare pad gets a bare pad.

The refusal is the one worth stating. ``append_terminated`` exists
because an unterminated tail costs the next record, so a caller handing
over an unterminated payload is planting the very defect, one write
early. Appending a newline for them would make the helper correct and
the caller silently wrong; the ``ValueError`` puts the mistake where it
was made.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kstrl.appendio import (
    JOURNAL_REPAIR_EVENT,
    append_records,
    append_terminated,
    appending,
    handle_ends_without_newline,
    open_for_append,
)
from tests.helpers.rootperms import skip_as_root

REPAIR = '{"event_type":"' + JOURNAL_REPAIR_EVENT + '"}\n'


class TestTheTerminatorIsRequired:
    def test_an_unterminated_payload_is_refused(self, tmp_path: Path) -> None:
        """The caller's mistake is raised at the caller, not absorbed.

        A helper that appended the missing newline itself would leave
        every caller free to keep getting it wrong, and the next one
        would copy the call that looked fine.
        """
        path = tmp_path / "log.jsonl"
        with pytest.raises(ValueError, match="must end in a newline"):
            append_records(path, '{"a":1}', repair=REPAIR)

    def test_the_refusal_happens_before_anything_lands(self, tmp_path: Path) -> None:
        """Nothing is written, so a caught ValueError leaves no half-record."""
        path = tmp_path / "log.jsonl"
        path.write_text('{"a":1}\n', encoding="utf-8")
        with pytest.raises(ValueError):
            append_records(path, "no terminator", repair=REPAIR)
        assert path.read_text(encoding="utf-8") == '{"a":1}\n'


class TestAnEmptyAppendIsNotARepair:
    def test_an_empty_payload_writes_nothing_to_a_torn_file(self, tmp_path: Path) -> None:
        """No record to protect means no repair, and no repair row.

        A repair triggered by an empty append would record an incident
        for a write that never happened, and would do it once per
        caller that happens to have nothing to say.
        """
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n{"tor')
        assert append_records(path, "", repair=REPAIR) is False
        assert path.read_bytes() == b'{"a":1}\n{"tor'

    def test_an_empty_payload_is_not_a_terminator_error(self, tmp_path: Path) -> None:
        """An empty string ends in no newline and is still legal.

        The terminator check and the nothing-to-write check overlap on
        exactly this input, and the order matters: the empty payload
        must reach the second one.
        """
        assert append_records(tmp_path / "log.jsonl", "", repair=REPAIR) is False


class TestTheRepairIsTheCallersChoice:
    def test_a_row_repair_lands_between_the_pad_and_the_payload(
        self,
        tmp_path: Path,
    ) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n{"tor')
        assert append_records(path, '{"b":2}\n', repair=REPAIR) is True
        assert path.read_bytes() == b'{"a":1}\n{"tor\n' + REPAIR.encode() + b'{"b":2}\n'

    def test_an_empty_repair_is_a_bare_pad(self, tmp_path: Path) -> None:
        """What the inbox and experiments.tsv ask for: a newline, no row.

        Pinned rather than left to the row test above, because "" is a
        distinct branch and a helper that wrote ``"\\n\\n"`` for it
        would still pass that one.
        """
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n{"tor')
        assert append_records(path, '{"b":2}\n', repair="") is True
        assert path.read_bytes() == b'{"a":1}\n{"tor\n{"b":2}\n'

    def test_an_intact_file_is_appended_to_byte_for_byte(self, tmp_path: Path) -> None:
        """No pad, no row, when there was no tear.

        The control for every test above: a helper that padded
        unconditionally would repair a torn file correctly and corrupt
        every ordinary append with a blank line and a false incident.
        """
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n')
        assert append_records(path, '{"b":2}\n', repair=REPAIR) is False
        assert path.read_bytes() == b'{"a":1}\n{"b":2}\n'

    def test_a_file_that_does_not_exist_yet_is_created_untouched(
        self,
        tmp_path: Path,
    ) -> None:
        """An empty file is not a torn file, so the first record is first."""
        path = tmp_path / "log.jsonl"
        assert append_records(path, '{"a":1}\n', repair=REPAIR) is False
        assert path.read_bytes() == b'{"a":1}\n'


class TestTheHandleForm:
    """``events.JsonlSink`` holds its handle open, so it uses this pair."""

    def test_the_probe_and_the_append_go_through_one_handle(self, tmp_path: Path) -> None:
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n{"tor')
        with appending(path) as handle:
            assert handle_ends_without_newline(handle) is True
            assert append_terminated(handle, '{"b":2}\n', repair=REPAIR) is True
            # The probe already consumed the tear, so a second append on
            # the SAME handle must not repair again.
            assert append_terminated(handle, '{"c":3}\n', repair=REPAIR) is False
        assert path.read_bytes().endswith(b'{"b":2}\n{"c":3}\n')

    @skip_as_root
    def test_open_for_append_raises_on_a_file_it_cannot_read(self, tmp_path: Path) -> None:
        """The point of ``"a+b"``: write-only is refused, not probed blind.

        Round 1 of review on #327 found the opposite shape fail-OPEN: a
        path-taking probe that answered False on every ``OSError``
        reported a mode-0200 file as "not torn" and appended to it.
        """
        path = tmp_path / "log.jsonl"
        path.write_bytes(b'{"a":1}\n')
        path.chmod(0o200)
        try:
            with pytest.raises(OSError):
                open_for_append(path).close()
        finally:
            path.chmod(0o600)

    def test_no_directory_is_created_for_the_caller(self, tmp_path: Path) -> None:
        """``mkdir`` belongs to the caller; a missing parent raises.

        Every caller already creates its own directory, several of them
        while creating sibling files in the same call, so a mkdir here
        would be a second one nobody asked for. Pinned because the
        absence of a call is invisible in a diff.
        """
        with pytest.raises(OSError):
            append_records(tmp_path / "nope" / "log.jsonl", "x\n", repair=REPAIR)
