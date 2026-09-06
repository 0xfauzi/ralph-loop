"""Every way a kstrl.toml can fail to parse, as real bytes.

One table, imported by the loader tests and by the CLI seam tests, so
that a fault discovered at one level is asserted at both. #318 shipped
three times because that was not true, and because each fix named a
ceiling one notch too low:

    round   handler added        what still escaped
    0       TOMLDecodeError      UnicodeDecodeError (a ValueError)
    1       UnicodeDecodeError   plain ValueError (4301-digit integer)
    2       ValueError           RecursionError (a RuntimeError)
    3       Exception            nothing that is about the document

Every row below is a fault that a shipped version of `load_toml_document`
converted into a raw traceback on thirteen of kstrl's sixteen commands.

REAL BYTES, NEVER A STUB
------------------------
Each entry is the file content, not the exception. All three defects were
about WHICH exception `tomllib.load` actually raises, so a fixture that
supplies the exception itself would have passed in every broken state and
proved nothing. The cost of that fidelity is one temp file per case,
measured at well under a millisecond.
"""

from __future__ import annotations

import sys

import pytest

from kstrl.config import UNPARSEABLE_TOML_MESSAGE

#: Syntactically broken: an unterminated table header. Raises
#: ``tomllib.TOMLDecodeError``, which carries a line and column.
MALFORMED_TOML = b"[verify\ntest_command = 'pytest'\n"

#: Syntactically perfect and not utf-8: one 0xe9, the byte an editor set
#: to ISO-8859-1 writes for an e-acute. ``tomllib.load`` decodes the
#: stream itself before it lexes anything, so this raises
#: ``UnicodeDecodeError`` - a ``ValueError``, and NOT a
#: ``TOMLDecodeError``. That is #318 round 1.
NON_UTF8_TOML = b'[agent]\nname = "\xe9"\n'

#: True unless the interpreter has the integer-string limit DISABLED.
#:
#: ``PYTHONINTMAXSTRDIGITS=0`` (and ``sys.set_int_max_str_digits(0)``)
#: are supported settings that switch the limit off entirely, and
#: ``sys.get_int_max_str_digits()`` then returns 0. There is no digit
#: count that fails in that configuration, so the fault does not exist
#: to be tested and its cases skip rather than fail. Measured: without
#: this, ``PYTHONINTMAXSTRDIGITS=0 pytest tests/test_config_toml.py``
#: was 3 failed / 23 passed.
INT_LIMIT_ENABLED = sys.get_int_max_str_digits() != 0

#: Valid utf-8, valid TOML grammar, and still unparseable: an integer
#: literal one digit past ``sys.get_int_max_str_digits()`` (4300 by
#: default since 3.11). ``int()`` refuses to build it and raises a PLAIN
#: ``ValueError`` - neither ``TOMLDecodeError`` nor
#: ``UnicodeDecodeError`` - so it walked past both handlers round 1
#: shipped. That is #318 round 2.
#:
#: Sized from the live limit rather than a hardcoded 4301 so it stays one
#: digit over the line if an interpreter or a caller moves it; when the
#: limit is OFF, :data:`INT_LIMIT_ENABLED` skips the cases instead,
#: because then no size works.
_INT_DIGITS = sys.get_int_max_str_digits() + 1
INT_LIMIT_TOML = b"[run]\nmax_iterations = " + b"9" * _INT_DIGITS + b"\n"

#: How deep to nest. ``tomllib`` parses arrays by recursive descent, so
#: nesting past the interpreter's recursion headroom raises
#: ``RecursionError`` - which derives from ``RuntimeError``, NOT
#: ``ValueError``, and so walked past everything round 2 shipped. That is
#: #318 round 3.
#:
#: Measured by binary search on this machine at the default limit: the
#: first depth that raises is 497 from a one-frame caller and 490
#: through ``ks status``. There is no one threshold. The caller's own
#: stack sets it, and the CLI parse that raises runs 16 frames deep,
#: which at roughly two interpreter frames per array level is the 7
#: levels the two numbers differ by. Scaled off the live recursion limit rather
#: than fixed at 600, so a run with a raised limit still nests past it:
#: any depth above the limit exhausts the stack whatever the limit is,
#: which is also why there is no ``max(600, ...)``
#: floor - it could never bind. Inline tables recurse identically;
#: arrays are used because they are terser per level. Measured cost of
#: building it: 0.21 us, 2205 bytes.
_NEST_DEPTH = sys.getrecursionlimit() + 100

#: Valid utf-8, and every bracket balanced: this is not a syntax error.
#: The parser gives up on its own stack, not on the grammar.
DEEP_NEST_TOML = b"a = " + b"[" * _NEST_DEPTH + b"]" * _NEST_DEPTH + b"\n"

#: What the loader's catch-all says, IMPORTED from the production
#: constant rather than restated. ``tests/test_config_toml.py`` asserts
#: on this string's ABSENCE from the specific handlers' messages, which
#: is half of how the handler order is pinned - and an absence assertion
#: against a stale literal passes vacuously instead of failing. Same
#: reason ``agents.proc.TIMEOUT_MESSAGE_PREFIX`` is imported by its tests
#: rather than repeated in them.
BROAD_FRAGMENT = UNPARSEABLE_TOML_MESSAGE

#: The one source: ``(name, file bytes, fragment, skip reason or None)``.
#: Private because every caller wants one of the views below rather than
#: all four fields, and a parametrize whose test never uses a field is
#: how an unused argument gets written.
_FAULTS: list[tuple[str, bytes, str, str | None]] = [
    ("syntax", MALFORMED_TOML, "Invalid TOML", None),
    ("encoding", NON_UTF8_TOML, "not valid UTF-8", None),
    (
        "int_limit",
        INT_LIMIT_TOML,
        BROAD_FRAGMENT,
        None if INT_LIMIT_ENABLED else "integer-string limit disabled on this interpreter",
    ),
    ("deep_nest", DEEP_NEST_TOML, BROAD_FRAGMENT, None),
]

#: ``(file bytes, expected message fragment)`` as pytest params, ids and
#: skip marks attached. The fragment is what makes the table an ORDERING
#: assertion as well as a coverage one: move the loader's broad handler
#: above the specific ones and the first two entries stop matching their
#: fragment and start matching :data:`BROAD_FRAGMENT`.
TOML_PARSE_FAULTS = [
    pytest.param(
        body,
        fragment,
        id=name,
        marks=[pytest.mark.skipif(skip is not None, reason=skip or "")],
    )
    for name, body, fragment, skip in _FAULTS
]

#: ``(name, bytes, fragment)`` for the faults that RUN on this
#: interpreter, for the one test that has to compare across all of them
#: in a single body rather than through parametrize.
ACTIVE_FAULTS: list[tuple[str, bytes, str]] = [
    (name, body, fragment) for name, body, fragment, skip in _FAULTS if skip is None
]

#: Every fragment any handler can produce.
#:
#: This is what makes a fault's message assertable EXCLUSIVELY - "says
#: its own fragment and none of the others" - rather than only
#: inclusively. The distinction is the whole ordering guarantee: move the
#: broad handler above the specific ones and each message still contains
#: SOMETHING, so an inclusive assertion is only caught on the wrong
#: fragment, and an assertion merely that the messages differ catches
#: nothing at all. (It cannot: every message interpolates the file path,
#: and the faults are written to different paths. A first cut asserted
#: exactly that and was vacuous - it passed with all handlers collapsed
#: into one.)
ALL_FRAGMENTS: frozenset[str] = frozenset(frag for _, _, frag, _ in _FAULTS)
