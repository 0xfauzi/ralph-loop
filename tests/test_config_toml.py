"""Tests for the kstrl.config TOML loader."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from kstrl.config import PATH_KEYS, ConfigError, KstrlConfig, load_toml_document
from tests.helpers.bad_toml import (
    ACTIVE_FAULTS,
    ALL_FRAGMENTS,
    BROAD_FRAGMENT,
    DEEP_NEST_TOML,
    INT_LIMIT_ENABLED,
    INT_LIMIT_TOML,
    TOML_PARSE_FAULTS,
)


def _write_toml(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# from_toml: mapping table
# ---------------------------------------------------------------------------


def test_from_toml_maps_agent_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[agent]
type = "codex"
command = "my-agent --stdin"
model = "o3"
reasoning_effort = "high"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.agent_type == "codex"
    assert config.agent_cmd == "my-agent --stdin"
    assert config.model == "o3"
    assert config.model_reasoning_effort == "high"


def test_from_toml_maps_run_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 42
sleep_seconds = 5
interactive = true
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.max_iterations == 42
    assert config.sleep_seconds == 5.0
    assert config.interactive is True


def test_from_toml_maps_paths_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[paths]
prompt = "custom/prompt.md"
prd = "custom/prd.json"
progress = "custom/progress.txt"
codebase_map = "custom/map.md"
golden_patterns = "custom/golden.md"
allowed = ["src/", "tests/"]
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.prompt_file == tmp_path / "custom/prompt.md"
    assert config.prd_file == tmp_path / "custom/prd.json"
    assert config.progress_file == tmp_path / "custom/progress.txt"
    assert config.codebase_map_file == tmp_path / "custom/map.md"
    assert config.golden_patterns_file == tmp_path / "custom/golden.md"
    assert config.allowed_paths == ["src/", "tests/"]


def test_every_path_key_names_a_real_field() -> None:
    """PATH_KEYS drives ``setattr``, and ``setattr`` on a typo invents an
    attribute rather than raising: the overlay would then silently write
    to a field nothing reads. Dataclass fields, not ``hasattr``, because
    an earlier row's typo would already have created the attribute."""
    declared = {f.name for f in dataclasses.fields(KstrlConfig)}
    assert declared >= {field_name for _key, _env, field_name in PATH_KEYS}


def test_from_toml_maps_git_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[git]
branch = "feature/x"
auto_checkout = false
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.kstrl_branch == "feature/x"
    assert config.kstrl_branch_explicit is True
    assert config.auto_checkout is False


def test_from_toml_maps_ui_section(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[ui]
ascii = true
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.ascii_only is True


def test_from_toml_empty_file_uses_defaults(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(toml_path, "")
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.max_iterations == 10
    assert config.sleep_seconds == 2.0
    assert config.agent_type is None


def test_from_toml_missing_file_uses_defaults(tmp_path: Path) -> None:
    config = KstrlConfig.from_toml(tmp_path / "nonexistent.toml", tmp_path)
    assert config.max_iterations == 10
    assert config.agent_cmd is None


def test_from_toml_malformed_raises_clear_error(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(toml_path, "this is not = valid = toml = [\n")
    with pytest.raises(ValueError, match="Invalid TOML"):
        KstrlConfig.from_toml(toml_path, tmp_path)


@pytest.mark.parametrize(("body", "fragment"), TOML_PARSE_FAULTS)
def test_load_toml_document_reports_every_parse_fault_as_a_config_error(
    tmp_path: Path,
    body: bytes,
    fragment: str,
) -> None:
    """#318, both rounds. Each of these escaped as a raw traceback at
    some point: the encoding fault past a handler naming only
    ``TOMLDecodeError``, and the integer-limit fault past that AND past
    the ``UnicodeDecodeError`` handler added to catch the first.

    Real bytes and real digits on disk, never a patched decoder or a
    raising stub. The whole defect both times was which exception the
    standard library actually raises here, so a test that supplies the
    exception itself would have passed in both broken states.
    """
    toml_path = tmp_path / "kstrl.toml"
    toml_path.write_bytes(body)

    with pytest.raises(ConfigError) as caught:
        load_toml_document(toml_path)

    message = str(caught.value)
    # Which file, in every one of them: the operator may have more than
    # one checkout and the parser's own message names none of them.
    assert str(toml_path) in message
    # EXCLUSIVELY its own fragment. ``fragment in message`` alone would
    # pass for a message that also carried another handler's line, which
    # is what a mis-ordered ladder produces; see
    # ``test_the_broad_handler_must_come_last``.
    assert {f for f in ALL_FRAGMENTS if f in message} == {fragment}
    # The cause survives, so the parser's own detail - a line and
    # column, a byte offset, a digit count, a recursion limit - is still
    # there to find. Asserted as ``Exception`` and not ``ValueError``,
    # because ``RecursionError`` is a ``RuntimeError``: narrowing this
    # to ``ValueError`` is the exact assumption that let round 3
    # through, and it would fail here rather than pass.
    assert isinstance(caught.value.__cause__, Exception)


def test_the_broad_handler_must_come_last(tmp_path: Path) -> None:
    """The one ordering constraint among the handlers, pinned so that
    violating it fails.

    ``TOMLDecodeError`` and ``UnicodeDecodeError`` are SIBLINGS - both
    derive from ``ValueError``, neither from the other - so swapping
    those two changes nothing, and the round-1 test that claimed to pin
    "the handler order" passed with them reversed. It pinned nothing.

    The broad ``except Exception`` is the real constraint: it is a
    supertype of every clause above it, so moved above one it swallows
    that fault and relabels it as an unspecified parse failure, taking
    the remedy with it. This asserts the property directly - each fault
    keeps its OWN message - rather than asserting the source order,
    which a test cannot see.

    It exists beside the parametrized test above, which carries the same
    exclusivity check per fault, because a failure that reads "you moved
    the broad handler" is worth more than several reading "wrong
    fragment".

    Watched failing with the handler moved, with ``__pycache__`` purged
    and ``PYTHONDONTWRITEBYTECODE=1``: permuting handlers leaves the file
    byte-identical, and two writes inside the same mtime second are
    served from a stale ``.pyc``, so the round-2 run of this proof was
    not sound even though its conclusion held.
    """
    messages: dict[str, str] = {}
    for name, body, _ in ACTIVE_FAULTS:
        toml_path = tmp_path / f"{name}.toml"
        toml_path.write_bytes(body)
        with pytest.raises(ConfigError) as caught:
            load_toml_document(toml_path)
        messages[name] = str(caught.value)

    # The faults with an established cause say what it is, and do NOT
    # fall through to the catch-all's deliberately vaguer line.
    assert BROAD_FRAGMENT not in messages["syntax"]
    assert BROAD_FRAGMENT not in messages["encoding"]
    # The ones with no established cause are the only ones that get it.
    assert BROAD_FRAGMENT in messages["deep_nest"]
    # Each message carries exactly one fragment, so no two rungs have
    # collapsed into each other. NOT ``len(set(messages.values())) == N``,
    # which a first cut asserted and which is VACUOUS: every message
    # interpolates the file path and the faults are written to different
    # paths, so it passes even with all handlers collapsed into one.
    # Measured that way, not reasoned about.
    assert [{f for f in ALL_FRAGMENTS if f in m} for m in messages.values()] == [
        {fragment} for _, _, fragment in ACTIVE_FAULTS
    ]


@pytest.mark.skipif(not INT_LIMIT_ENABLED, reason="integer-string limit disabled")
def test_the_catch_all_does_not_diagnose_a_cause_it_has_not_established(
    tmp_path: Path,
) -> None:
    """Fail closed without overclaiming.

    The round-1 fix reported a cause the handler knew (`not valid
    UTF-8`). Widening that message to cover the whole family would tell
    an operator whose file is perfectly good utf-8 to re-save it as
    utf-8 - a silent semantic substitution one step on from swallowing
    the error.
    """
    toml_path = tmp_path / "kstrl.toml"
    toml_path.write_bytes(INT_LIMIT_TOML)

    with pytest.raises(ConfigError) as caught:
        load_toml_document(toml_path)

    message = str(caught.value)
    assert "UTF-8" not in message
    # What it DOES pass on is the parser's own line, which is the
    # actionable part and the only part it can honestly claim.
    assert "4300 digits" in message


def test_a_recursion_error_is_reported_not_raised(tmp_path: Path) -> None:
    """#318 round 3, stated as its own case rather than only as a table
    row, because the CLASS of the escaping exception is the whole point.

    ``RecursionError`` derives from ``RuntimeError``, NOT from
    ``ValueError``, so every handler rounds 1 and 2 shipped let it
    through - and round 2's docstring, AST guard and CLAUDE.md line all
    asserted that ``ValueError`` was the whole class. The assertion is on
    the ``__cause__``'s type, so narrowing the handler back to
    ``ValueError`` fails here loudly instead of reintroducing the escape
    quietly. It deliberately does NOT also assert
    ``not isinstance(cause, ValueError)``: the line above has already
    fixed the class, so that would assert CPython's lattice rather than
    this code, and a test that cannot fail is not a test.
    """
    toml_path = tmp_path / "kstrl.toml"
    toml_path.write_bytes(DEEP_NEST_TOML)

    with pytest.raises(ConfigError) as caught:
        load_toml_document(toml_path)

    assert isinstance(caught.value.__cause__, RecursionError)
    assert BROAD_FRAGMENT in str(caught.value)


def test_open_failures_are_not_relabelled_as_parse_failures() -> None:
    """The I/O is outside the guard, so its faults keep their own type.

    A path with an embedded null byte raises ``ValueError`` from the read
    itself - before any bytes exist to parse - and the round-2 catch-all
    reported it as "could not be parsed as TOML" for a file it had never
    opened. That is the same overclaiming the catch-all's wording is
    written to avoid, arriving through the try block's extent instead of
    through its message.
    """
    with pytest.raises(ValueError) as caught:
        load_toml_document(Path("bad\x00path.toml"))

    assert not isinstance(caught.value, ConfigError)
    assert BROAD_FRAGMENT not in str(caught.value)


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_an_unreadable_file_still_raises_oserror_not_configerror(tmp_path: Path, kind: str) -> None:
    """The contract ``config_preflight.SURFACE_REJECTIONS`` is built on.

    Before the catch-all widened past ``ValueError`` this held by
    accident, because ``OSError`` is not one. It now holds because the
    read happens BEFORE the guard, so no widening of the guard can reach
    it. An intermediate draft instead re-raised from an ``except
    OSError:`` clause inside the guard; this test was written claiming
    to pin that clause, and did not, because the read already failed one
    line above the ``try``. Hoisting the I/O out made the clause
    unreachable and the honest fix was to delete it, not to keep a
    special case no test could enter.

    Two kinds, because one file-shaped fault is not the population:
    a path that does not exist, and a path that exists and is not a
    regular file.
    """
    if kind == "missing":
        target = tmp_path / "nope" / "kstrl.toml"
    else:
        target = tmp_path / "kstrl.toml"
        target.mkdir()

    with pytest.raises(OSError) as caught:
        load_toml_document(target)

    assert not isinstance(caught.value, ConfigError)


def test_from_toml_resolves_absolute_paths(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    abs_prompt = tmp_path / "elsewhere" / "p.md"
    _write_toml(
        toml_path,
        f"""
[paths]
prompt = "{abs_prompt}"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.prompt_file == abs_prompt


def test_from_toml_ignores_unknown_keys(tmp_path: Path) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[agent]
type = "claude"
unknown_field = "ignored"

[unknown_section]
foo = "bar"
""",
    )
    config = KstrlConfig.from_toml(toml_path, tmp_path)
    assert config.agent_type == "claude"


# ---------------------------------------------------------------------------
# load: env > toml > defaults precedence
# ---------------------------------------------------------------------------


def test_load_env_overrides_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 25

[agent]
model = "sonnet"
""",
    )
    monkeypatch.setenv("MAX_ITERATIONS", "99")
    monkeypatch.setenv("MODEL", "opus")
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 99
    assert config.model == "opus"


def test_paths_golden_patterns_config_env_beats_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R10.8's key follows the [paths] precedence: env > toml > default."""
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[paths]
golden_patterns = "from-toml/golden.md"
""",
    )
    monkeypatch.delenv("KSTRL_GOLDEN_PATTERNS_FILE", raising=False)
    assert KstrlConfig.load(tmp_path).golden_patterns_file == tmp_path / "from-toml/golden.md"

    monkeypatch.setenv("KSTRL_GOLDEN_PATTERNS_FILE", "from-env/golden.md")
    assert KstrlConfig.load(tmp_path).golden_patterns_file == tmp_path / "from-env/golden.md"


def test_golden_patterns_defaults_anchored_against_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("KSTRL_GOLDEN_PATTERNS_FILE", raising=False)
    expected = tmp_path / "scripts/kstrl/golden-patterns.md"
    assert KstrlConfig.load(tmp_path).golden_patterns_file == expected
    assert KstrlConfig.from_env(tmp_path).golden_patterns_file == expected
    assert KstrlConfig.from_toml(tmp_path / "kstrl.toml", tmp_path).golden_patterns_file == expected


def test_an_empty_golden_patterns_key_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The [paths] rule for every key: empty means unset, not an error."""
    monkeypatch.delenv("KSTRL_GOLDEN_PATTERNS_FILE", raising=False)
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(toml_path, '\n[paths]\ngolden_patterns = ""\n')
    expected = tmp_path / "scripts/kstrl/golden-patterns.md"
    assert KstrlConfig.load(tmp_path).golden_patterns_file == expected


def test_load_toml_wins_over_defaults_when_env_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Clear env vars that might leak from the test environment
    for var in ("MAX_ITERATIONS", "MODEL", "SLEEP_SECONDS", "INTERACTIVE"):
        monkeypatch.delenv(var, raising=False)
    toml_path = tmp_path / "kstrl.toml"
    _write_toml(
        toml_path,
        """
[run]
max_iterations = 25
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 25


def test_load_defaults_when_no_toml_and_no_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "MAX_ITERATIONS",
        "MODEL",
        "SLEEP_SECONDS",
        "INTERACTIVE",
        "ALLOWED_PATHS",
        "AGENT_CMD",
        "MODEL_REASONING_EFFORT",
        "KSTRL_AGENT_TYPE",
        "KSTRL_BRANCH",
        "KSTRL_ASCII",
    ):
        monkeypatch.delenv(var, raising=False)
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 10
    assert config.sleep_seconds == 2.0
    assert config.agent_type is None
    assert config.agent_cmd is None
    assert config.kstrl_branch is None
    assert config.kstrl_branch_explicit is False


def test_load_auto_discovers_kstrl_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "kstrl.toml",
        """
[run]
max_iterations = 7
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 7


def test_load_missing_toml_falls_back_silently(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    config = KstrlConfig.load(tmp_path)
    assert config.max_iterations == 10


def test_load_env_branch_marks_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("KSTRL_BRANCH", "")
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch == ""
    assert config.kstrl_branch_explicit is True


def test_load_env_paths_resolved_against_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROMPT_FILE", "custom/prompt.md")
    config = KstrlConfig.load(tmp_path)
    assert config.prompt_file == tmp_path / "custom/prompt.md"


def test_load_toml_empty_branch_does_not_mark_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kstrl.toml.example documents `branch = ""` as 'empty = use PRD
    branchName'. An empty TOML branch must therefore NOT mark explicit,
    so loop.determine_branch falls through to PRD lookup instead of
    skipping checkout. Env var KSTRL_BRANCH="" retains its historical
    explicit-skip meaning - that path is tested elsewhere."""
    for var in ("KSTRL_BRANCH",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "kstrl.toml",
        """
[git]
branch = ""
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch is None
    assert config.kstrl_branch_explicit is False


def test_load_toml_nonempty_branch_marks_explicit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("KSTRL_BRANCH",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "kstrl.toml",
        """
[git]
branch = "feature/foo"
""",
    )
    config = KstrlConfig.load(tmp_path)
    assert config.kstrl_branch == "feature/foo"
    assert config.kstrl_branch_explicit is True


# ---------------------------------------------------------------------------
# from_env: backwards compatibility
# ---------------------------------------------------------------------------


def test_from_env_still_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_ITERATIONS", "13")
    monkeypatch.setenv("MODEL", "haiku")
    config = KstrlConfig.from_env(tmp_path)
    assert config.max_iterations == 13
    assert config.model == "haiku"
    assert config.prompt_file == tmp_path / "scripts/kstrl/prompt.md"


def test_from_env_does_not_read_toml(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in ("MAX_ITERATIONS",):
        monkeypatch.delenv(var, raising=False)
    _write_toml(
        tmp_path / "kstrl.toml",
        """
[run]
max_iterations = 999
""",
    )
    # Change cwd to tmp_path so any auto-discovery would pick up the toml
    monkeypatch.chdir(tmp_path)
    config = KstrlConfig.from_env(tmp_path)
    # from_env must ignore toml
    assert config.max_iterations == 10
