"""Phase 1: Mechanical verification - independent checks after agent execution."""

from __future__ import annotations

import os
import py_compile
import re
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from kstrl import git, licensing

if TYPE_CHECKING:
    from kstrl.fixtures import FixturesConfig
from kstrl.adequacy import (
    AdequacyConfig,
    evaluate_layer0,
    is_test_path,
    layer0_blocks,
)
from kstrl.config import component_progress_path, relative_to_root
from kstrl.findings import Finding
from kstrl.gateparse import (
    GATE_LINT,
    GATE_TEST,
    GATE_TYPECHECK,
    parse_gate_output,
    validate_tool,
)
from kstrl.guards import path_is_allowed
from kstrl.parsers import (
    ParsedOutput,
    add_source_context,
    generate_fix_hint,
)
from kstrl.policy import (
    PolicyConfig,
    PolicyConfigError,
    PolicyViolation,
    classify_license,
    evaluate_policy,
)
from kstrl.prd import PRD
from kstrl.procdispose import drain_or_abandon
from kstrl.procgroup import signal_process_tree
from kstrl.statedir import STATE_DIR_NAME

# R2.6 env scrub: verification subprocesses execute agent-authored code
# (the project's tests, linters run over agent files, CLI fixtures), so
# they must never inherit the harness's secrets. Allowlist, not denylist:
# only names below (or matching a prefix below) pass through, everything
# else - ANTHROPIC_API_KEY, OPENAI_API_KEY, cloud credentials, gh tokens -
# is dropped. The set was determined empirically: `uv run pytest` with a
# fresh venv succeeds under env -i with only PATH/HOME/TMPDIR/TERM/LANG
# (uv locates its cache via HOME); the rest are the locale, venv, uv, and
# CPython knobs a project's own commands legitimately consume, plus the
# XDG cache/data paths uv honors when set.
SCRUB_ENV_ALLOWED_NAMES: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "LANG",
        "TMPDIR",
        "TERM",
        "VIRTUAL_ENV",
        "CI",
        "XDG_CACHE_HOME",
        "XDG_DATA_HOME",
    }
)
SCRUB_ENV_ALLOWED_PREFIXES: tuple[str, ...] = ("LC_", "UV_", "PYTHON")

# Belt over the allowlist's braces: an allowed prefix must never smuggle a
# secret through (UV_PUBLISH_TOKEN matches UV_*). Any name containing one
# of these fragments is dropped even when the allowlist admits it.
_SCRUB_ENV_SENSITIVE_FRAGMENTS: tuple[str, ...] = (
    "API_KEY",
    "SECRET",
    "TOKEN",
    "PASSWORD",
    "CREDENTIAL",
)


def scrubbed_subprocess_env() -> dict[str, str]:
    """Allowlist-filtered copy of ``os.environ`` for verification subprocesses."""
    env: dict[str, str] = {}
    for name, value in os.environ.items():
        if name not in SCRUB_ENV_ALLOWED_NAMES and not name.startswith(SCRUB_ENV_ALLOWED_PREFIXES):
            continue
        if any(frag in name for frag in _SCRUB_ENV_SENSITIVE_FRAGMENTS):
            continue
        env[name] = value
    return env


_SCRUB_TERM_GRACE_SECONDS = 5.0


def _signal_process_group(proc: subprocess.Popen[str], sig: signal.Signals) -> None:
    """Signal the child's whole process group, direct-child fallback.

    A one-line forward to :func:`kstrl.procgroup.signal_process_tree`,
    kept as a name because ``tests/test_hitl_env_scrub.py`` pins this
    module's timeout behaviour through it and because the name says what
    the verification path is doing at the point it does it.

    #308 lifted the pid/pgid GUARD out of here and left the routine
    around it, so ``os.killpg`` itself stayed spelled in this module and
    in ``agents.proc`` as well as in ``procgroup``. #329 is what that
    costs: three spellings is how a fourth arrives unguarded. The whole
    routine now has one home.
    """
    signal_process_tree(proc, sig)


def run_scrubbed(
    cmd: str | list[str],
    *,
    cwd: Path,
    timeout: float,
    term_grace: float = _SCRUB_TERM_GRACE_SECONDS,
) -> subprocess.CompletedProcess[str]:
    """Run a verification subprocess: scrubbed env, own process group.

    Drop-in for the ``subprocess.run(..., capture_output=True, text=True,
    timeout=...)`` calls verification used to make, with two differences
    (R2.6): the child gets :func:`scrubbed_subprocess_env` instead of the
    harness environment, and on timeout the ENTIRE process group is
    signalled (SIGTERM, grace, SIGKILL) so a test that backgrounds a
    server cannot leak it past the deadline. A string ``cmd`` runs through
    the shell exactly as before; a list does not.

    Raises :class:`subprocess.TimeoutExpired` after the group is dead so
    existing callers' timeout handling keeps working unchanged.

    THE TIMEOUT PATH LETS GO THROUGH ``procdispose`` (#326). It used to
    drain the pipes itself and, when that drain expired, set
    ``stdout, stderr = "", ""`` and drop the child on the floor: no
    close of the two pipe ends, and no register, so the only thing left
    holding the pid was ``Popen.__del__``. That is not a fallback under
    ``PYTHONWARNINGS=error``, which is a setting this codebase already
    records crashing a daemon: ``__del__`` calls ``_warn`` BEFORE
    ``_active.append`` (CPython 3.12.8 ``subprocess.py`` lines 1139 and
    1145), the warn raises, ``__del__`` aborts, and the child stays a
    zombie for the life of the process. This is the widest window of the
    three sites that had it - it needs no D-state child, only something
    outside the group holding a pipe write end, which a forked
    grandchild does routinely, and it runs once per verification command
    per iteration rather than once per timed-out run.
    """
    proc = subprocess.Popen(
        cmd,
        shell=isinstance(cmd, str),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=scrubbed_subprocess_env(),
        start_new_session=True,
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            proc.wait(timeout=term_grace)
        except subprocess.TimeoutExpired:
            pass
        # SIGKILL the group even when the direct child honored SIGTERM: a
        # grandchild that ignored it can hold the pipes open and would
        # otherwise block the drain below indefinitely.
        _signal_process_group(proc, signal.SIGKILL)
        stdout, stderr = drain_or_abandon(proc, term_grace)
        raise subprocess.TimeoutExpired(
            cmd,
            timeout,
            output=stdout,
            stderr=stderr,
        ) from None
    except BaseException:
        # The rule `procgroup._read_ps` already states and this module
        # did not: every exit that is not a completed read leaves a
        # child behind, so every one of them goes through the same
        # disposal. Catching only `TimeoutExpired` made this the widest
        # remaining hole of the #326 class rather than a fixed site - a
        # KeyboardInterrupt out of `ks verify`, or a MemoryError on a
        # capture big enough to matter, left the child unsignalled,
        # unreaped, unregistered and holding both pipe ends, on the
        # highest-frequency spawn in the factory.
        drain_or_abandon(proc, term_grace)
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)


@dataclass
class CheckResult:
    """Result of a single verification check."""

    name: str
    passed: bool
    message: str = ""
    details: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    parsed: ParsedOutput | None = None
    # R8.1: typed findings this mechanical check produced, lifted into the
    # component's finding stream by the pipeline so a machine-made gate
    # decision lands in the audit trail (PR body, journal) and not only in
    # the retry context. Empty for checks that emit prose only.
    findings: list[Finding] = field(default_factory=list)


#: Why a check that was ASKED FOR produced no measurement. Stable
#: tokens: they reach `ks sense --json` and `events.jsonl`, so a reader
#: keys on these and not on the prose beside them.
#:
#: A check the operator did not ask for - ``[verify] mutation_testing``
#: left false - records nothing at all. That is the line the sidecar
#: draws, and it is what keeps the default quiet: silence is a complete
#: answer to a question nobody asked, and an incomplete one to a
#: question they did.
NOT_MEASURED_READ_ONLY = "read_only"
NOT_MEASURED_TOOL_MISSING = "tool_missing"
NOT_MEASURED_NO_TARGET = "no_target"
NOT_MEASURED_TIMED_OUT = "timed_out"
NOT_MEASURED_COMMAND_FAILED = "command_failed"
NOT_MEASURED_NO_MUTANTS = "no_mutants"


@dataclass(frozen=True)
class NotMeasured:
    """A check that was asked for and produced no measurement (#306).

    The SIDECAR. Deliberately not a :class:`CheckResult`: it never
    reaches ``checks``, so ``all(c.passed ...)``, ``report_lines``'
    verdict column, ``ks sense --json``'s ``checks`` array and
    :func:`kstrl.review.build_review_prompt` cannot read it as a pass -
    which is the whole of #306. Equally it never reaches
    :meth:`VerificationResult.as_context`, so it is not retry context:
    no engineer iteration is spent on a missing binary it cannot
    install.

    What it buys back is the diagnostic the omission alone destroyed.
    Absence from ``checks`` is honest but mute, and SEVEN states produce
    that absence, which an operator who set ``mutation_testing = true``
    cannot otherwise tell apart from a working gate. Six of them carry
    one of these records and are separated by ``reason``, one of the
    ``NOT_MEASURED_*`` constants above. The seventh, the check being
    turned off, records nothing at all, on purpose: a question nobody
    asked needs no answer.

    ``detail`` is prose for a human and is never parsed.
    """

    check: str
    reason: str
    detail: str

    def as_line(self) -> str:
        """The report-table rendering, for :meth:`VerificationResult.report_lines`.

        Not every terminal surface: the factory's Phase 1 warning
        prefixes the component id and names the reason, because it is
        one line inside a multi-component run rather than a row under a
        table that already says which component it is.
        """
        return f"  {self.check}  not measured  {self.detail}"

    def as_token(self) -> str:
        """The ``events.jsonl`` rendering: ``"<check>:<reason>"``.

        Here rather than in the emitters because there are two of them,
        in :mod:`kstrl.pipeline` and :mod:`kstrl.feature_verify`, and a
        format spelled twice is one an edit can change in one place
        only. Same ``<check>:<code>`` shape
        :func:`kstrl.evolution.split_signature` already reads, so a
        consumer of that file meets one convention rather than two.
        """
        return f"{self.check}:{self.reason}"

    def to_dict(self) -> dict[str, str]:
        """The ``ks sense --json`` rendering."""
        return {"check": self.check, "reason": self.reason, "detail": self.detail}


def _capped_detail_lines(check: CheckResult, limit: int | None) -> list[str]:
    """``check``'s details, indented, truncated to ``limit`` if given.

    The truncation is never silent: what is dropped is counted on a final
    line, because a report that quietly shows you 12 of 400 failures is a
    report you would act on wrongly.
    """
    lines = [f"      {line}" for detail in check.details for line in detail.splitlines()]
    if limit is None or len(lines) <= limit:
        return lines
    return [*lines[:limit], f"      ... {len(lines) - limit} more line(s) not shown"]


@dataclass
class VerificationResult:
    """Aggregated result of all mechanical checks."""

    passed: bool
    checks: list[CheckResult] = field(default_factory=list)
    #: Checks that were asked for and measured nothing (#306). The
    #: sidecar: see :class:`NotMeasured` for why it is beside ``checks``
    #: rather than in it.
    not_measured: list[NotMeasured] = field(default_factory=list)

    def as_context(self) -> str:
        """Format failures for injection into retry prompt."""
        lines: list[str] = []
        for check in self.checks:
            if not check.passed:
                lines.append(f"- {check.name}: FAIL - {check.message}")
                for detail in check.details[:10]:
                    lines.append(f"  {detail}")
        return "\n".join(lines)

    def report_lines(
        self,
        *,
        durations: bool = True,
        max_detail_lines: int | None = None,
    ) -> list[str]:
        """One line per check, then the indented details of each failure.

        The TERMINAL rendering of this object, in one place: ``ks sense``
        and ``ks feature``'s #288 report print the same table, and before
        this existed they printed it from two copies of the same
        f-string.

        ``durations=False`` drops the wall-clock column. `ks feature`
        needs that: its narration sits inside a longer flow that
        ``tests/test_feature_run.py`` compares BYTE FOR BYTE between a
        recorded and an unrecorded run, and a timing makes two runs of
        the same work disagree. The figure is not lost there - it goes on
        the ``VerificationResultEvent``.

        ``max_detail_lines`` caps the details PER CHECK and appends a
        line saying how many were dropped, so a truncation is never
        silent. `ks feature` needs that too, and for a different reason:
        under the embedded TUI every one of these lines becomes a
        ``Log`` event on the run bus, and a failing gate's details are
        ``ParsedOutput.format_for_prompt`` - every parsed failure with a
        source-context snippet. A 40-failure suite is hundreds of events
        per report, up to ``2 + repair_max_runs`` times a run, which is
        the event-stream flood ``commandrun._StreamFilterSink`` exists to
        prevent. ``as_context`` already truncates at 10 for the same
        reason. None (the default, and ``ks sense``) prints everything:
        there the measurement IS the whole output.
        """
        width = max((len(check.name) for check in self.checks), default=0)
        lines: list[str] = []
        for check in self.checks:
            verdict = "pass" if check.passed else "FAIL"
            timing = f"  ({check.duration_seconds:.2f}s)" if durations else ""
            lines.append(f"  {check.name.ljust(width)}  {verdict}  {check.message}{timing}")
            if check.passed:
                continue
            lines.extend(_capped_detail_lines(check, max_detail_lines))
        # The sidecar, below the table and outside it (#306). Rendered
        # here rather than by each caller for the reason the table is:
        # `ks sense` and `ks feature` must not be able to disagree about
        # whether they mention what was not measured. No verdict column
        # and no duration - there is no verdict, and nothing was timed.
        lines.extend(gap.as_line() for gap in self.not_measured)
        return lines


def _optional_str(value: object) -> str | None:
    """A toml scalar as a string, with the empty string meaning unset."""
    return str(value) or None


#: Every live ``[verify]`` toml key and how its value is coerced onto the
#: dataclass. A table rather than a per-key ``if``: the chain it replaced
#: was fourteen near-identical branches, and its cyclomatic complexity
#: was already twice the repo's ratchet limit before #258 added three
#: more keys to it. Order is the dataclass's, so a reader can diff the
#: two lists by eye. Anything absent from this table is not a toml key.
_VERIFY_TOML_FIELDS: tuple[tuple[str, Callable[[Any], object]], ...] = (
    ("test_command", _optional_str),
    ("typecheck_command", _optional_str),
    ("lint_command", _optional_str),
    # validate_tool already maps the empty string to None (auto) and
    # raises on anything it does not recognise, so it needs no coercion
    # in front of it.
    ("test_tool", partial(validate_tool, GATE_TEST)),
    ("typecheck_tool", partial(validate_tool, GATE_TYPECHECK)),
    ("lint_tool", partial(validate_tool, GATE_LINT)),
    ("check_diff_scope", bool),
    ("check_bad_patterns", bool),
    ("dead_code_cleanup", bool),
    ("dead_code_command", _optional_str),
    ("mutation_testing", bool),
    ("mutation_threshold", float),
    ("mutation_timeout", float),
    ("subprocess_timeout", float),
    ("require_self_critique", bool),
    ("self_critique_min_bullets", int),
    ("progress_file_path", _optional_str),
)


@dataclass
class VerifyConfig:
    """Configuration for mechanical verification."""

    test_command: str | None = None
    typecheck_command: str | None = None
    lint_command: str | None = None
    # Which parser reads each gate's output (#258). None is auto: every
    # parser registered for the gate runs and their findings are unioned,
    # which is what makes a chained command
    # (`uv run pytest && npm run test`) yield BOTH toolchains' failures.
    # Set one to pin the gate to a single parser. Accepted values are
    # kstrl.gateparse.GATE_TOOLS[<gate>]; anything else raises on load.
    test_tool: str | None = None
    typecheck_tool: str | None = None
    lint_tool: str | None = None
    check_diff_scope: bool = True
    check_bad_patterns: bool = True
    dead_code_cleanup: bool = False
    dead_code_command: str | None = None
    mutation_testing: bool = False
    mutation_threshold: float = 50.0
    mutation_timeout: float = 600.0
    subprocess_timeout: float = 300.0
    # Mechanical enforcement of the engineer prompt's "## Self-Critique"
    # mandate. Off by default to keep this opt-in; set to True (or
    # KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE=1) to fail Phase 1 when an
    # iteration's progress.txt entry omits the block.
    require_self_critique: bool = False
    self_critique_min_bullets: int = 3
    # Where check_self_critique looks for the engineer's progress log.
    # None (the default) derives it from the component's PRD
    # (config.component_progress_path), which is where the engineer was
    # actually told to write and the only location inside the
    # component's allowedPaths. An explicit value wins for every
    # component. It is None-defaulted rather than carrying a separate
    # "was it set?" flag because every scalar field of this dataclass is
    # a documented kstrl.toml key (scripts/gen_docs.py probes for that).
    progress_file_path: str | None = None

    @classmethod
    def from_env(cls) -> VerifyConfig:
        """Load verify config from environment variables."""
        return cls(
            test_command=os.environ.get("KSTRL_VERIFY_TEST_CMD"),
            typecheck_command=os.environ.get("KSTRL_VERIFY_TYPECHECK_CMD"),
            lint_command=os.environ.get("KSTRL_VERIFY_LINT_CMD"),
            test_tool=validate_tool(GATE_TEST, os.environ.get("KSTRL_VERIFY_TEST_TOOL")),
            typecheck_tool=validate_tool(
                GATE_TYPECHECK, os.environ.get("KSTRL_VERIFY_TYPECHECK_TOOL")
            ),
            lint_tool=validate_tool(GATE_LINT, os.environ.get("KSTRL_VERIFY_LINT_TOOL")),
            dead_code_cleanup=os.environ.get("KSTRL_DEAD_CODE_CLEANUP", "") == "1",
            dead_code_command=os.environ.get("KSTRL_DEAD_CODE_CMD"),
            mutation_testing=os.environ.get("KSTRL_MUTATION_TESTING", "") == "1",
            mutation_threshold=float(os.environ.get("KSTRL_MUTATION_THRESHOLD", "50")),
            mutation_timeout=float(os.environ.get("KSTRL_MUTATION_TIMEOUT", "600")),
            subprocess_timeout=float(os.environ.get("KSTRL_TIMEOUT_VERIFY", "300")),
            require_self_critique=os.environ.get("KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE", "") == "1",
            self_critique_min_bullets=int(
                os.environ.get("KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS", "3"),
            ),
            progress_file_path=os.environ.get("KSTRL_VERIFY_PROGRESS_FILE"),
        )

    @classmethod
    def load(cls, root_dir: Path | None = None) -> VerifyConfig:
        """Load verify config with precedence: env > toml > defaults."""
        from kstrl.config import load_toml_section, resolve_config_file

        if root_dir is None:
            root_dir = Path.cwd()
        config = cls()
        section = load_toml_section(resolve_config_file(root_dir), "verify")
        for key, coerce in _VERIFY_TOML_FIELDS:
            if key in section:
                setattr(config, key, coerce(section[key]))
        # Env overrides. Each var is applied only when it is explicitly
        # set in the environment: the previous compare-against-default
        # heuristic silently dropped an env value that happened to equal
        # the dataclass default (e.g. KSTRL_MUTATION_THRESHOLD=50 could
        # not override a toml mutation_threshold), breaking the
        # env-beats-toml precedence contract (R2.1).
        env = cls.from_env()
        env_var_to_field = {
            "KSTRL_VERIFY_TEST_CMD": "test_command",
            "KSTRL_VERIFY_TYPECHECK_CMD": "typecheck_command",
            "KSTRL_VERIFY_LINT_CMD": "lint_command",
            "KSTRL_VERIFY_TEST_TOOL": "test_tool",
            "KSTRL_VERIFY_TYPECHECK_TOOL": "typecheck_tool",
            "KSTRL_VERIFY_LINT_TOOL": "lint_tool",
            "KSTRL_DEAD_CODE_CLEANUP": "dead_code_cleanup",
            "KSTRL_DEAD_CODE_CMD": "dead_code_command",
            "KSTRL_MUTATION_TESTING": "mutation_testing",
            "KSTRL_MUTATION_THRESHOLD": "mutation_threshold",
            "KSTRL_MUTATION_TIMEOUT": "mutation_timeout",
            "KSTRL_TIMEOUT_VERIFY": "subprocess_timeout",
            "KSTRL_VERIFY_REQUIRE_SELF_CRITIQUE": "require_self_critique",
            "KSTRL_VERIFY_SELF_CRITIQUE_MIN_BULLETS": "self_critique_min_bullets",
            "KSTRL_VERIFY_PROGRESS_FILE": "progress_file_path",
        }
        for env_var, field_name in env_var_to_field.items():
            if env_var in os.environ:
                setattr(config, field_name, getattr(env, field_name))
        return config


# Patterns that suggest secrets in source code
SECRET_PATTERNS = [
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"sk-[a-zA-Z0-9]{20,}"),  # OpenAI/Stripe key
    re.compile(r"ghp_[a-zA-Z0-9]{36}"),  # GitHub PAT
    re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----"),  # Private keys
    re.compile(r"xox[bpoas]-[a-zA-Z0-9-]+"),  # Slack tokens
]


# Engineer prompt mandates the EXACT heading `## Self-Critique`.
# Accept also `- **Self-Critique:**` (common bullet-in-list form) and
# `## Self Critique` (loose hyphen-space variant). Reject prose like
# "the self-critique above" so we don't false-positive on body text.
# Both forms must START the line after at most a list marker + whitespace.
_SELF_CRITIQUE_HEADING_RE = re.compile(
    r"""^
    (?:
        \#{2,3}\s+                  # H2 / H3: '## ' or '### '
      | [\-*]\s+\*{2}\s*            # '- **' or '* **'
    )
    Self[-\s]Critique
    (?:
        \s*\*{2}                    # '**' (close bold)
      | \s*:                        # ':'
      | \s*\*{2}\s*:                # '**:'
      | \s*$                        # end-of-line
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An iteration entry boundary in progress.txt. The engineer prompt's
# documented format starts each appended entry with
# `## [YYYY-MM-DD] - [Story ID]`; agents also commonly write
# `## Iteration N`. Exactly two hashes: H3 sub-headings inside an
# entry must not be mistaken for a new entry.
_ITERATION_HEADING_RE = re.compile(
    r"""^\#\#\s+
    (?:
        \[?\d{4}-\d{2}-\d{2}        # '## [YYYY-MM-DD] - ...' (documented form)
      | Iteration\b                 # '## Iteration N' (loose variant)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# An UNINDENTED bullet opening with a closed bold label, e.g.
# `- **Learnings:**` or `- **Interpretations** (only if ...): ...`.
# In the engineer prompt's entry format these are sibling sections of
# `- **Self-Critique:**`, so one of them terminates the bullet count.
# Applied to the raw line: the Self-Critique block's own nested bullets
# are indented and therefore never match.
_SECTION_BULLET_RE = re.compile(r"^[\-*]\s+\*{2}[^*]+\*{2}")

# Thematic break: the engineer prompt's entry format ends each entry
# with `---`.
_ENTRY_SEPARATOR_RE = re.compile(r"^-{3,}$")


def _self_critique_text(progress_path: Path, start: float) -> str | CheckResult:
    """The progress file's text, or the failing check explaining why not.

    Two handlers because the remedies differ: "could not read" sends the
    operator to the file's permissions, and this file opened fine - the
    agent wrote bytes that are not UTF-8, which is a fact about the
    agent's output rather than about the disk. Before #320 the decode was
    not caught at all and a Phase 1 gate died with a traceback instead of
    reporting red.

    Split out of :func:`check_self_critique` so the second handler does
    not push that function past the complexity ratchets.
    """
    try:
        return progress_path.read_text(encoding="utf-8")
    except OSError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Could not read progress file: {exc}",
            duration_seconds=time.monotonic() - start,
        )
    except UnicodeDecodeError as exc:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=f"Progress file is not valid UTF-8: {exc}",
            duration_seconds=time.monotonic() - start,
        )


def check_self_critique(
    progress_path: Path,
    min_bullets: int = 3,
) -> CheckResult:
    """Confirm the CURRENT (latest) progress.txt entry contains a
    Self-Critique block with at least ``min_bullets`` bullet points.

    Shape check only (H4): this verifies that a Self-Critique block of
    the right shape exists in the right place. It does NOT verify the
    substance of the bullets - vacuous-but-plausible failure modes
    pass. Substance is the reviewer's job.

    Format assumption (from the engineer prompt's Progress Format):
    each iteration appends an entry starting with an H2 heading of the
    form `## [YYYY-MM-DD] - [Story ID]` (the loose `## Iteration N`
    variant is also recognized), containing `- **Self-Critique:**` (or
    `## Self-Critique`) followed by bullets, sibling bold-label
    sections such as `- **Interpretations:**`, and a closing `---`.

    The check first locates the latest iteration boundary (the LAST
    line matching ``_ITERATION_HEADING_RE``), then requires a
    Self-Critique heading within that entry - a block written by an
    EARLIER iteration does not satisfy the check for the current one.
    If no iteration heading exists anywhere, the whole file is treated
    as a single entry (fallback for free-form progress files; per-
    iteration association is not possible there).

    Bullet counting stops at the next `##` heading, a `---` entry
    separator, or an unindented bold-label bullet (a sibling section
    like `- **Interpretations:**`), so bullets belonging to later
    sections do not inflate the count. Consequence of the format
    assumption: critique bullets themselves must either be indented
    under the `- **Self-Critique:**` bullet (the documented format) or
    not open with a bold label, otherwise they read as a sibling
    section and the check fails loudly rather than over-counting.

    Without this mechanical check, the engineer prompt's mandate to
    list >=3 failure modes can silently rot - the only enforcement
    path otherwise is the reviewer noticing, which is unreliable.
    """
    start = time.monotonic()
    text = _self_critique_text(progress_path, start)
    if isinstance(text, CheckResult):
        return text

    lines = text.splitlines()
    # Locate the latest iteration entry: entries are appended, so the
    # LAST iteration heading starts the current iteration's entry.
    entry_start = 0
    entry_found = False
    for i in range(len(lines) - 1, -1, -1):
        if _ITERATION_HEADING_RE.match(lines[i]):
            entry_start = i
            entry_found = True
            break

    # Find the LAST self-critique heading WITHIN the latest entry, so
    # an earlier iteration's block cannot satisfy the current one and
    # repeated blocks inside one entry resolve to the newest.
    heading_idx: int | None = None
    for i in range(len(lines) - 1, entry_start - 1, -1):
        if _SELF_CRITIQUE_HEADING_RE.match(lines[i]):
            heading_idx = i
            break

    if heading_idx is None:
        where = (
            f"in the latest iteration entry (line {entry_start + 1}: "
            f"{lines[entry_start].strip()[:60]!r})"
            if entry_found
            else "in progress file"
        )
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"No '## Self-Critique' block found {where}. "
                "Engineer prompt mandates >=3 failure-mode bullets "
                "before declaring done."
            ),
            duration_seconds=time.monotonic() - start,
        )

    # Count bullets after the heading until the entry's content ends:
    # next `##` heading, `---` separator, or a sibling bold-label
    # bullet section (e.g. `- **Interpretations:**`).
    bullet_count = 0
    bullet_lines: list[str] = []
    for line in lines[heading_idx + 1 :]:
        stripped = line.strip()
        # Stop at next major heading
        if stripped.startswith("##"):
            break
        # Stop at the entry separator
        if _ENTRY_SEPARATOR_RE.match(stripped):
            break
        # Stop at the next sibling section: an UNINDENTED bold-label
        # bullet (matched on the raw line so the block's own indented
        # bullets never terminate the count).
        if _SECTION_BULLET_RE.match(line):
            break
        # Count substantive bullets (require non-trivial content after the marker)
        if stripped.startswith("- ") or stripped.startswith("* "):
            body = stripped[2:].strip()
            if body and not body.lower().startswith(("tbd", "todo", "n/a")):
                bullet_count += 1
                bullet_lines.append(body[:80])

    if bullet_count < min_bullets:
        return CheckResult(
            name="self_critique",
            passed=False,
            message=(
                f"Self-Critique block has {bullet_count} bullets; minimum required is {min_bullets}"
            ),
            details=bullet_lines,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="self_critique",
        passed=True,
        message=f"{bullet_count} failure modes listed",
        duration_seconds=time.monotonic() - start,
    )


def _tamper_changes(prd: PRD, pre_run_prd_path: Path | None) -> list[str]:
    """How ``prd`` differs from the pre-run copy in ways no engineer may.

    Defence in depth for #264's carve-out, kept deliberately after #269
    made the SCOPE half of this comparison unnecessary. The plan-time
    snapshot (``kstrl.scope``) settles what a component may write, so an
    ``allowedPaths`` the agent edits is inert and is no longer compared:
    see that module for why comparing a value the agent can rewrite is
    the weaker answer.

    What the snapshot does NOT cover is everything else that reads this
    file, and a lot does: ``check_prd_stories`` below, the approved
    fixtures oracle, the acceptance criteria handed to the reviewer, the
    R10.3 set-point sensor. None can be served from a snapshot, because
    the agent setting ``passes`` is the whole job, so the live file has
    to be trusted and a comparison is the only answer available for it.
    Drop this and an agent can delete an acceptance criterion or neuter
    an executable oracle and pass a gate it authored.

    WHICH fields are the engineer's to write is the PRD's own business
    and lives in ``PRD.tamper_changes``, beside the fields it pins.

    Empty when there is nothing to compare, which is not the same as
    waving something through:

    - ``pre_run_prd_path`` is None. The caller has no trustworthy copy
      to offer: ``ks sense`` judges an operator's own working tree.
    - The pre-run copy will not load. A harness or operator condition,
      not something an agent can arrange from inside its worktree.
    - It is the SAME file, which is ``use_worktrees=False``: both reads
      return the same document and the comparison is empty by
      arithmetic rather than by a special case. That mode has no
      isolation boundary, so this check cannot be what gives it one.
      Scope is the part of the answer that does survive there, because
      #269 reads it before the agent starts.
    """
    if pre_run_prd_path is None:
        return []
    try:
        pre_run = PRD.load(pre_run_prd_path)
    except (OSError, ValueError):
        return []
    return prd.tamper_changes(pre_run)


def check_prd_stories(prd_path: Path, pre_run_prd_path: Path | None = None) -> CheckResult:
    """Re-read PRD from disk and verify all stories have passes=true.

    ``pre_run_prd_path`` (#269) is the copy of the same PRD the run
    started with, which lives outside every worktree and so is not
    agent-writable. Given one, this check also refuses a PRD the
    component rewrote in its own favour (``_tamper_changes``).

    This is the check that carries that refusal, rather than
    ``diff_scope``, for two reasons. It is a statement about the
    STORIES, which is what this check reads and what a rewrite attacks;
    scope stopped being the question when #269 made the plan-time
    snapshot the only scope source. And ``diff_scope`` is switchable off
    (``[verify] check_diff_scope``), while this one runs whenever there
    is a PRD at all: defence in depth an unrelated toggle can disable is
    not defence in depth.
    """
    start = time.monotonic()
    try:
        prd = PRD.load(prd_path)
    except Exception as exc:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"Failed to load PRD: {exc}",
            duration_seconds=time.monotonic() - start,
        )

    tampered = _tamper_changes(prd, pre_run_prd_path)
    if tampered:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message="The PRD is not the one this run started with; failing closed",
            details=[
                f"It {'; '.join(tampered)}. A component may set `passes` "
                "and `notes` on its own stories and nothing else: it may "
                "not rewrite the criteria or the fixtures it is judged "
                "against.",
                "Every gate that reads this file - these stories, the "
                "approved fixtures, the criteria the reviewer is given - "
                "is judging a document the component rewrote. Restore it "
                "to what the run started with; do not treat this as "
                "permission to change what the component is measured "
                "against.",
            ],
            duration_seconds=time.monotonic() - start,
        )

    failing = [s for s in prd.user_stories if not s.passes]
    if failing:
        return CheckResult(
            name="prd_stories",
            passed=False,
            message=f"{len(failing)} stories not marked as passing",
            details=[f"{s.id}: {s.title}" for s in failing],
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="prd_stories",
        passed=True,
        message=f"All {len(prd.user_stories)} stories passing",
        duration_seconds=time.monotonic() - start,
    )


# ---------------------------------------------------------------------------
# Resolved verification commands (#261)
# ---------------------------------------------------------------------------
#
# The single source of truth for "what will Phase 1 actually run". Both
# the gate (``check_test_suite`` / ``check_typecheck`` / ``check_linter``)
# and the engineer prompt (``loop.run_loop``) answer that question by
# calling the resolvers below, so the agent cannot be told a command the
# gate will not run.
#
# ``ks init`` used to scaffold a second, hardcoded copy of these commands
# into the generated CLAUDE.md. Every copy disagreed with the gate from
# the moment init finished, and loop.run_loop prepends CLAUDE.md into the
# engineer prompt, so the harness mechanically fed the agent the wrong
# commands. The copy is gone; this module is the only source.

#: Gate default when ``[verify] test_command`` is unset.
DEFAULT_TEST_COMMAND = "uv run pytest"

#: Gate default when ``[verify] lint_command`` is unset.
DEFAULT_LINT_COMMAND = "uv run ruff check ."

#: Gate fallback when ``[verify] typecheck_command`` is unset AND the
#: project does not scope mypy itself. ``_default_typecheck_command``
#: prefers ``uv run mypy`` (no path) whenever pyproject.toml does.
DEFAULT_TYPECHECK_COMMAND = "uv run mypy ."

#: What ``_default_typecheck_command`` uses instead when the project has
#: scoped mypy via ``[tool.mypy] files`` or ``packages``.
SCOPED_TYPECHECK_COMMAND = "uv run mypy"

# Harness-authored instruction text injected into the engineer prompt on
# every iteration, so it is enrolled in the H3 version/hash snapshot
# (tests/test_prompt_versions.py) exactly like DEFAULT_PROMPT. Only the
# TEMPLATE is snapshotted: the three command values are the operator's,
# interpolated at run time, and H3 cannot and should not pin those.
VERIFY_COMMANDS_PROMPT_VERSION = "1.0.0"

VERIFY_COMMANDS_PROMPT = """\
# Verification Commands (resolved by kstrl)

These are the exact commands kstrl's mechanical verification gate runs on your
work, resolved from this project's `kstrl.toml` `[verify]` section. Run them
yourself before you report a story complete. They are authoritative: ignore any
other verification command list, including one written in the project context
above.

- Test: `{test}`
- Typecheck: `{typecheck}`
- Lint: `{lint}`

A command may chain several toolchains. Run all of it."""


def _default_typecheck_command(cwd: Path) -> str:
    """Choose a sensible default mypy invocation for ``cwd``.

    Generic ``uv run mypy .`` is hostile to projects whose pyproject.toml
    deliberately scopes mypy via ``[tool.mypy] files`` or ``packages``:
    the ``.`` argument overrides those settings and pulls in test files
    or vendored code that the project never intended to typecheck. When
    the project has configured its own mypy scope, defer to it by
    invoking ``uv run mypy`` with no path argument (mypy then reads the
    config). When no such config is present, fall back to the broad
    ``uv run mypy .`` so a green-field project still gets coverage.

    This is the Gap 2 fix from the end-to-end factory validation run:
    the factory's verify command was overriding the project's own
    typecheck scope, leading to Phase 1 failures on diffs that were
    actually fine. Gap 2 landed on the gate and not on ``ks init``, which
    kept scaffolding ``mypy src/ --strict`` into CLAUDE.md - the very
    shape it identified as wrong. #261 closed that half.
    """
    import tomllib

    pyproject = cwd / "pyproject.toml"
    if pyproject.is_file():
        # The read is outside the guard for the same reason it is in
        # ``config.load_toml_document``: an I/O fault is not a parse
        # fault. Here it makes no difference to the caller, since both
        # end at the same default, but a rule applied at one of two
        # sites and not the other is a rule the next author has to guess
        # at.
        try:
            raw = pyproject.read_bytes()
        except OSError:
            return DEFAULT_TYPECHECK_COMMAND
        try:
            data = tomllib.loads(raw.decode())
        except Exception:
            # ``Exception``, not an enumeration of what tomllib is
            # believed to raise: see ``kstrl.config.load_toml_document``
            # for the argument and ``tests/test_toml_readers.py`` for
            # the guard. The one fact local to THIS site is that a
            # pyproject.toml is not the operator's kstrl.toml, so it
            # fails to a documented default rather than to an error,
            # which is why catching the whole class costs nothing here.
            return DEFAULT_TYPECHECK_COMMAND
        mypy_section = data.get("tool", {}).get("mypy", {})
        if isinstance(mypy_section, dict):
            # Acknowledged edge case: this heuristic does not consult
            # ``[[tool.mypy.overrides]]`` (per-module relaxation) or
            # modules-only configs. If a project relaxes via overrides
            # but doesn't set ``files``/``packages``, the broad
            # ``uv run mypy .`` default would override the relaxation.
            # Real-world rare. Users can always override explicitly via
            # ``--typecheck-command`` or env var.
            if mypy_section.get("files") or mypy_section.get("packages"):
                return SCOPED_TYPECHECK_COMMAND
    return DEFAULT_TYPECHECK_COMMAND


def resolve_test_command(command: str | None) -> str:
    """The exact test command Phase 1 will run."""
    return command or DEFAULT_TEST_COMMAND


def resolve_typecheck_command(command: str | None, cwd: Path) -> str:
    """The exact typecheck command Phase 1 will run in ``cwd``."""
    return command or _default_typecheck_command(cwd)


def resolve_lint_command(command: str | None) -> str:
    """The exact lint command Phase 1 will run."""
    return command or DEFAULT_LINT_COMMAND


@dataclass(frozen=True)
class ResolvedVerifyCommands:
    """The concrete commands Phase 1 runs, after config and defaults.

    Every field is a shell command line, so a chained polyglot command
    (``uv run pytest -q && cd web && npm run test``) survives verbatim:
    the resolver never splits or rewrites what the operator configured.
    """

    test: str
    typecheck: str
    lint: str

    def format_for_prompt(self) -> str:
        """Render the block injected into the engineer prompt.

        Stated as authoritative because an agent working in a project
        scaffolded before #261 may also be shown a stale CLAUDE.md list,
        and has to know which one binds.
        """
        return VERIFY_COMMANDS_PROMPT.format(
            test=self.test,
            typecheck=self.typecheck,
            lint=self.lint,
        )


def pin_verify_commands(config: VerifyConfig, cwd: Path) -> VerifyConfig:
    """A copy of ``config`` whose three command fields are already resolved.

    Resolution is not a pure function of the config: ``resolve_typecheck_command``
    falls back to ``_default_typecheck_command(cwd)``, which re-reads
    ``cwd/pyproject.toml`` and answers ``uv run mypy`` when
    ``[tool.mypy] files`` or ``packages`` is present and ``uv run mypy .``
    when it is not. Adding a mypy scope is an ordinary engineer story, so
    a caller that resolves more than once during a run can get two
    different commands from one config (#288 review round 2).

    Pinning is what makes every later resolution the identity: once the
    fields are non-None, ``resolve_*_command`` returns them unchanged. So
    the report's announcement, the command that actually runs, and the
    ``VERIFY_COMMANDS_PROMPT`` block ``build_project_context`` renders
    for the engineer are provably one string per command for the whole
    run, rather than three independent reads that agree by luck.

    Do NOT use this on the factory's per-component path without thinking:
    there each component has its own worktree, and the right pyproject to
    resolve against is that worktree's, not the caller's ``cwd``.
    """
    resolved = resolve_verify_commands(config, cwd)
    return replace(
        config,
        test_command=resolved.test,
        typecheck_command=resolved.typecheck,
        lint_command=resolved.lint,
    )


def resolve_verify_commands(config: VerifyConfig, cwd: Path) -> ResolvedVerifyCommands:
    """Resolve ``config`` against ``cwd`` into the commands Phase 1 runs.

    ``cwd`` is the directory the gate will run in (the component's
    worktree under the factory), because the typecheck default is a
    function of that directory's pyproject.toml.
    """
    return ResolvedVerifyCommands(
        test=resolve_test_command(config.test_command),
        typecheck=resolve_typecheck_command(config.typecheck_command, cwd),
        lint=resolve_lint_command(config.lint_command),
    )


# A CLAUDE.md verification bullet in the shape ``ks init`` used to
# generate: ``- **Test**: `uv run pytest tests/ -v --tb=short```. Matched
# anywhere in the file rather than under a specific heading, because the
# heading text varies ("## Verification Commands", "## Verification
# commands") while the bullet shape does not.
_CLAUDE_MD_COMMAND_RE = re.compile(
    r"^\s*[-*]\s+\*{2}(Test|Typecheck|Lint)\*{2}\s*:\s*`([^`]+)`\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ScrubbedProjectContext:
    """CLAUDE.md text with stale verification bullets removed (#261)."""

    text: str
    #: One human-readable line per removed bullet, for ``ui.warn``.
    divergences: list[str]


def scrub_stale_verify_commands(
    claude_md: str,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext:
    """Drop CLAUDE.md verification bullets that disagree with the gate.

    Projects scaffolded before #261 carry a generated ``## Verification
    Commands`` section whose three bullets disagree with what the gate
    runs. ``loop.run_loop`` prepends CLAUDE.md into the engineer prompt,
    so those bullets are instructions the agent follows and then fails
    Phase 1 on.

    Removal is per-bullet and only when the stated command differs from
    the resolved one, so a project whose CLAUDE.md happens to be correct
    is left byte-identical, and surrounding prose always survives. The
    file on disk is never modified: this scrubs the in-memory copy that
    goes into the prompt, and every removal is reported so the operator
    can delete the stale section for good.
    """
    kept: list[str] = []
    divergences: list[str] = []
    by_label = {
        "test": commands.test,
        "typecheck": commands.typecheck,
        "lint": commands.lint,
    }
    # keepends: what survives is re-joined with "", so a file with CRLF
    # endings or no trailing newline round-trips byte for byte.
    for line in claude_md.splitlines(keepends=True):
        match = _CLAUDE_MD_COMMAND_RE.match(line)
        if match is None:
            kept.append(line)
            continue
        label = match.group(1).lower()
        stated = match.group(2).strip()
        resolved = by_label[label]
        if stated == resolved:
            kept.append(line)
            continue
        divergences.append(
            f"CLAUDE.md tells the agent to {label} with `{stated}`, but the "
            f"gate runs `{resolved}`. Dropping the stale line from the "
            f"engineer prompt; delete it from CLAUDE.md and set [verify] in "
            f"kstrl.toml instead."
        )
    return ScrubbedProjectContext(text="".join(kept), divergences=divergences)


def scrub_project_claude_md(
    root: Path,
    commands: ResolvedVerifyCommands,
) -> ScrubbedProjectContext | None:
    """``scrub_stale_verify_commands`` on ``root``'s CLAUDE.md, or None.

    None when the project has no readable CLAUDE.md. One place decides
    where the file lives and what an unreadable one means, because two
    callers need the same answer for different reasons: the engineer
    loop wants ``.text`` (the copy that goes into the prompt) and the
    factory preflight wants ``.divergences`` (what to tell the
    operator), and they had drifted into two different missing-file
    policies (#261).
    """
    try:
        claude_md = (root / "CLAUDE.md").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    return scrub_stale_verify_commands(claude_md, commands)


def _failed_gate_result(
    name: str,
    message: str,
    parsed: ParsedOutput,
    cmd: str,
    cwd: Path,
    start: float,
) -> CheckResult:
    """Enrich a parse and package it as the gate's failing CheckResult.

    One home for all three gates because the enrichment has to happen at
    every one of them and forgetting a step is SILENT: without
    ``parsed.command`` the prompt label falls back to the parser name,
    which is exactly the #258 mislabel returning unannounced.
    """
    parsed.command = cmd
    for failure in parsed.failures:
        # eslint's default formatter prints ABSOLUTE paths, so without
        # this the engineer is handed a path rooted in kstrl's throwaway
        # worktree: correct on disk, useless as an instruction, and not
        # the path its own tools use. Deliberately the FILE only. The
        # message is prose the tool wrote and may quote a path too
        # (measured: vitest's load errors do); rewriting a tool's own
        # sentences by string substitution is a different and less safe
        # mechanism than resolving a path, and the file is the field the
        # engineer acts on and add_source_context resolves.
        if failure.file:
            failure.file = relative_to_root(Path(failure.file), cwd)
        add_source_context(failure, cwd)
        if not failure.fix_hint:
            failure.fix_hint = generate_fix_hint(failure)
    return CheckResult(
        name=name,
        passed=False,
        message=message,
        details=parsed.format_for_prompt(),
        duration_seconds=time.monotonic() - start,
        parsed=parsed,
    )


def check_test_suite(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run the project's test suite independently.

    ``tool`` pins which parser reads the output; None runs every parser
    registered for the gate and unions what they find (#258).
    """
    start = time.monotonic()
    cmd = resolve_test_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TEST,
            passed=False,
            message=f"Test suite timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TEST,
            f"Tests failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TEST, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TEST,
        passed=True,
        message="Tests passed",
        duration_seconds=time.monotonic() - start,
    )


def check_typecheck(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run typecheck independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_typecheck_command(command, cwd)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_TYPECHECK,
            passed=False,
            message=f"Typecheck timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_TYPECHECK,
            f"Typecheck failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_TYPECHECK, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_TYPECHECK,
        passed=True,
        message="Typecheck passed",
        duration_seconds=time.monotonic() - start,
    )


def check_linter(
    cwd: Path,
    command: str | None = None,
    timeout: float = 300.0,
    tool: str | None = None,
) -> CheckResult:
    """Run linter independently. See ``check_test_suite`` for ``tool``."""
    start = time.monotonic()
    cmd = resolve_lint_command(command)

    try:
        result = run_scrubbed(cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return CheckResult(
            name=GATE_LINT,
            passed=False,
            message=f"Linter timed out after {timeout}s",
            duration_seconds=time.monotonic() - start,
        )

    if result.returncode != 0:
        output = (result.stdout + result.stderr).strip()
        return _failed_gate_result(
            GATE_LINT,
            f"Linter failed (exit code {result.returncode})",
            parse_gate_output(output, GATE_LINT, tool),
            cmd,
            cwd,
            start,
        )

    return CheckResult(
        name=GATE_LINT,
        passed=True,
        message="Linter passed",
        duration_seconds=time.monotonic() - start,
    )


def _diff_scope_details(
    base_branch: str,
    allowed_paths: list[str],
    harness_paths: list[str] | None,
    violations: list[str],
) -> list[str]:
    """Failure details for a diff that left its scope.

    R0.4: name the base branch and the FULL allowed-paths list. Without
    them the retry agent has to guess both; the recorded e2e run guessed
    `main` as base and reverted base-branch content with `git checkout
    main -- ...`, failing again. Base branch and allowed paths are single
    detail entries at the head of the list so
    ``VerificationResult.as_context()``'s ``details[:10]`` slice carries
    them into the retry prompt verbatim.

    #264: the harness carve-out is its own entry, never folded into the
    authored list. The operator has to be able to read what THEY
    authorised, and the retry agent has to know its own PRD and progress
    log are already in scope - telling it to stop writing those is the
    one instruction it cannot obey and still pass ``prd_stories``.
    """
    shown = violations[:15]
    violation_lines = [f"  - {v}" for v in shown]
    if len(violations) > len(shown):
        violation_lines.append(f"  ... and {len(violations) - len(shown)} more")
    harness_note = (
        [
            "Plus harness artifacts (kstrl's own files, already in "
            f"scope, no need to widen allowedPaths): {', '.join(harness_paths)}"
        ]
        if harness_paths
        else []
    )
    return [
        f"Base branch: {base_branch} "
        f"(scope is judged on `git diff {base_branch}...HEAD`; "
        f"do NOT `git checkout {base_branch} -- <path>`, revert only "
        "your own out-of-scope commits/edits)",
        f"Allowed paths (complete list): {', '.join(allowed_paths)}",
        *harness_note,
        # One multi-line entry so as_context()'s details[:10] slice
        # cannot drop violations or the truncation marker.
        "Files outside allowed scope:\n" + "\n".join(violation_lines),
    ]


#: The Phase 1 check name for mutation testing, and the ``check`` on
#: every :class:`NotMeasured` mutation testing produces.
#:
#: A constant rather than four literals for a reason that is measured,
#: not stylistic: ``tests/test_check_name_enrolment.py`` AST-walks
#: ``kstrl/`` for every name that can reach
#: ``evolution.category_for_check``, and it resolves literals and
#: module-level constants - not function-locals. Writing this name once
#: as ``name = "mutation_testing"`` inside
#: :func:`check_mutation_score` took it from the 19 names that walk
#: sees to 18, silently, because the walk fails on an unenrolled name
#: and cannot fail on one it cannot see. Same rule as the prompt walk
#: in #299: hoist it to a constant the guard can resolve.
MUTATION_TESTING_CHECK = "mutation_testing"


#: The Phase 1 check name for the vulture-or-``dead_code_command``
#: phase, and the ``check`` on every :class:`NotMeasured` it produces.
#:
#: It keeps the name the fused check had, and the new phase beside it
#: takes a new one, rather than the other way round. Three reasons, all
#: checkable. ``evolution.signatures_from_verification`` emits a
#: signature only for a FAILED check and the ruff phase has no failing
#: return, so every ``dead_code:*`` signature any journal could ever
#: carry came from this phase. This is the phase that reads
#: ``git diff``, so this is the one :data:`DIFF_DEPENDENT_CHECKS`
#: names. And ``[verify] dead_code_command`` replaces vulture outright,
#: so a name mentioning vulture would be wrong whenever the operator's
#: own detector runs.
DEAD_CODE_CHECK = "dead_code"

#: The Phase 1 check name for the ruff F401/F811/F841 phase (#335).
#:
#: New in the split, and NOT diff-dependent: ruff scans ``.``, so it has
#: an honest answer with no base to diff against. It is still suppressed
#: wherever ``[verify] dead_code_cleanup`` is turned off, which is what
#: ``narrow_to_undiffed`` does, because one toggle owns both phases.
DEAD_CODE_RUFF_CHECK = "dead_code_ruff"


#: The Phase 1 check name for "no trustworthy scope could be read".
#:
#: Deliberately NOT ``scope_source``, which is already taken in the same
#: substrate: ``events.ComponentScopeResolved.scope_source`` is a
#: payload FIELD naming which authority supplied a component's
#: allowlist (component_prd / run_flag / unconstrained / unresolved).
#: A check of that name reaches the same ``events.jsonl`` as a VALUE in
#: ``VerificationResultEvent.checks``, so one token would carry two
#: unrelated meanings for the dashboards that read that file.
SCOPE_UNREADABLE_CHECK = "scope_unreadable"


#: Opening words of the failure recorded when a component is refused for
#: an unreadable scope. Load bearing twice over, so it is a constant
#: rather than a literal: ``evolution._classify_check`` matches on it to
#: recover the check name from a manifest written by an earlier process,
#: and it is what an operator sees first in the inbox, the notification
#: and ``comp.error``.
SCOPE_UNREADABLE_ERROR_PREFIX = "Component scope could not be read; retrying cannot change it"


def scope_unreadable_error(cause: str) -> str:
    """The recorded error for an unreadable scope, carrying its cause.

    ``pipeline.fail`` writes this to ``comp.error``, the
    ``ComponentFailed`` event, ``notify.fire_first_failure`` and the
    HALTED_RUN inbox item's detail. A fixed string left all four saying
    only THAT the scope was unreadable, while the file to restore sat in
    the check's details, where none of them look.
    ``factory._preflight_component_scope`` names the file in its own
    refusal; every refusal for this cause should read alike.
    """
    return f"{SCOPE_UNREADABLE_ERROR_PREFIX}. {cause}"


#: Rendered in place of an empty ``allowed_paths_error``. A fail-closed
#: check must not pass on an ambiguous sentinel (round 2), and it must
#: not refuse while naming no cause either (round 1). It refuses, and
#: says the cause is missing.
NO_CAUSE_RECORDED = "(no cause recorded; the scope resolver supplied an empty error)"


def check_scope_unreadable(allowed_paths_error: str) -> CheckResult:
    """Report that no trustworthy scope could be established (R1.5, #294).

    Fails CLOSED: no allowlist could be read, so no diff can be proven
    in-scope, and silently skipping the guard is the hole R1.5 exists to
    close. Distinct from ``allowed_paths=None`` reaching
    ``check_diff_scope``, which means no scope was CONFIGURED -- a
    legitimate pass.

    Its own check, and not a branch of ``diff_scope``, because the two
    name different faults and the name is what a reader acts on (#294).
    ``diff_scope`` means "the diff touched files outside the allowlist",
    so its retry context is read as "narrow the diff". Here there was no
    allowlist to be outside of: it is resolved once at plan time from
    the pre-run checkout (``scope.ComponentScope``), which is OUTSIDE
    every worktree and fixed for the life of the run, so nothing the
    engineer writes can move this verdict.

    TWO producers, with different remedies, which is why the text points
    at the ``Error:`` line rather than asserting a cause:

    - ``ComponentScope.resolve`` could not read or parse the component's
      pre-run PRD. Restore that file.
    - ``RunScope.for_component`` had no snapshot for the component at
      all and returned its fail-closed stand-in. The PRD is fine; the
      manifest and the resolved run scope disagree about which
      components exist, which is a harness fault.

    An earlier version asserted the first cause unconditionally, so on
    the second it sent an operator to inspect a file that reads
    perfectly. That is round-1 finding 1 again: a remediation naming an
    action that cannot fix the failure.

    Neither remedy is ``--allowed-paths``. ``resolve`` returns
    ``unresolved`` BEFORE it consults the run-wide flag, on the argument
    that a scope nobody could read is not a scope that does not exist,
    so a run restarted with the flag hits the identical refusal.

    Carries an infrastructure ``Finding`` because this is the harness
    failing to establish its own input, not a judgement about the
    change. Without it a run that dies here leaves an empty finding
    stream, and every consumer using ``len(findings) == 0`` as "ran
    cleanly" reads a hard stop as clean.

    Whether it runs at all is ``_scope_checks``'s decision, and it is
    ungated there.
    """
    start = time.monotonic()
    cause = allowed_paths_error or NO_CAUSE_RECORDED
    return CheckResult(
        name=SCOPE_UNREADABLE_CHECK,
        passed=False,
        message="Scope could not be read at plan time; failing closed",
        details=[
            f"Error: {cause}",
            "The allowedPaths this component must be judged against "
            "could not be established before the run started, so no "
            "diff can be proven in-scope. This is NOT a diff violation, "
            "and NOT something an engineer can fix from inside the "
            "worktree: the scope is read from the pre-run checkout, "
            "outside this worktree, and is fixed for the life of the "
            "run, so neither narrowing nor widening the diff changes "
            "this verdict.",
            "The Error line above names which of two faults this is. A "
            "pre-run PRD that would not read or parse: restore that "
            "file in the main checkout and start a new run. No "
            "plan-time scope resolved for this component at all: the "
            "PRD is not the problem, the manifest and the run's "
            "resolved scope disagree about which components exist, and "
            "that is a harness fault to report rather than a file to "
            "repair. A run-wide --allowed-paths fixes neither: scope "
            "resolution refuses before it reaches the flag, so a re-run "
            "with it set fails identically.",
        ],
        findings=[
            Finding.infrastructure_error(
                "verify",
                f"component scope could not be established at plan time: {cause}",
            )
        ],
        duration_seconds=time.monotonic() - start,
    )


def check_diff_scope(
    cwd: Path,
    base_branch: str,
    allowed_paths: list[str] | None = None,
    *,
    harness_paths: list[str] | None = None,
) -> CheckResult:
    """Check that git diff is within expected scope.

    One question only: did the diff touch a file outside the allowlist?
    The allowlist not being READABLE is a different fault with a
    different audience, and it is ``check_scope_unreadable`` (#294).

    It no longer carries PRD TAMPERING either. That refusal moved to
    ``check_prd_stories`` when the plan-time snapshot took the scope
    question away from the worktree PRD: the file can still be rewritten
    and the stories still have to be defended, but the scope this check
    enforces is not something the rewrite can reach any more, so saying
    "scope could not be established" about it was untrue.

    ``harness_paths`` (#264) is kstrl's OWN per-component carve-out from
    ``config.component_harness_paths``: exact files kstrl's other checks
    require the agent to write (its PRD, its progress log, the codebase
    map). They widen the effective scope but are reported SEPARATELY, so
    the failure message still shows the operator what they authorised.
    They never create a scope where none was configured: with
    ``allowed_paths`` unset the check still passes unconditionally.

    Keyword-only, because #294 deleted an ``allowed_paths_error``
    parameter that sat in the 4th positional slot and this argument
    would otherwise have inherited it. Measured on the intermediate
    version: an unported caller passing the error string positionally
    got ``passed=True`` / "No scope constraints" where it intended a
    hard refusal, and with a non-empty ``allowed_paths`` the string
    splatted character by character into the effective allowlist. A
    silent fail-open is the one failure mode this check exists to
    prevent.
    """
    start = time.monotonic()

    if not allowed_paths:
        return CheckResult(
            name="diff_scope",
            passed=True,
            message="No scope constraints (allowed_paths not set)",
            duration_seconds=time.monotonic() - start,
        )

    changed = git.get_diff_names(base_branch, cwd)
    # #264: the authored scope plus kstrl's own per-component files. The
    # two lists stay separate all the way into the failure details: an
    # operator reading "outside allowed scope" must be able to tell what
    # they authorised from what the harness added on their behalf.
    #
    # Deliberately NOT guards.check_violations, which is the same
    # decision on the same inputs: it takes a set and returns sorted, and
    # the violation list is truncated to 15 for the retry prompt, so
    # sorting silently changes WHICH violations the retry agent is shown.
    # Git's order is the order the operator sees elsewhere; a cosmetic
    # de-duplication is not worth moving it.
    effective = [*allowed_paths, *(harness_paths or ())]
    violations = [f for f in changed if not path_is_allowed(f, effective)]

    if violations:
        details = _diff_scope_details(
            base_branch,
            allowed_paths,
            harness_paths,
            violations,
        )
        return CheckResult(
            name="diff_scope",
            passed=False,
            message=(
                f"{len(violations)} files outside allowed scope "
                f"(diff vs base branch '{base_branch}')"
            ),
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="diff_scope",
        passed=True,
        message=f"{len(changed)} files, all within scope",
        duration_seconds=time.monotonic() - start,
    )


def check_bad_patterns(cwd: Path, base_branch: str) -> CheckResult:
    """Scan changed files for obvious problems.

    The scan reads the tree and writes nothing into it. The syntax
    check earns that: ``py_compile.compile`` defaults its output to
    ``<dir>/__pycache__/<name>.pyc`` NEXT TO the source, so scanning
    used to leave bytecode behind - noise in the factory's own diff,
    and a write ``ks sense`` (R10.1) promises never to make. Directing
    ``cfile`` at a throwaway directory keeps the ``PyCompileError``
    type and message byte-identical; only the destination moves.
    """
    start = time.monotonic()
    issues: list[str] = []

    changed = git.get_diff_names(base_branch, cwd)
    py_files = [f for f in changed if f.endswith(".py")]

    with tempfile.TemporaryDirectory(prefix="kstrl-bytecode-") as bytecode_dir:
        # One reused destination: the content is never read back, only
        # the compile's success or failure is.
        cfile = os.path.join(bytecode_dir, "scan.pyc")
        for rel_path in py_files:
            full_path = cwd / rel_path
            if not full_path.exists():
                continue

            # Empty file check. utf-8 pinned, not left to the locale:
            # PEP 3120 makes utf-8 the default source encoding, so this
            # is the encoding the file is in, and reading it as cp1252
            # under a non-UTF-8 locale would silently change which
            # SECRET_PATTERNS matched below.
            content = full_path.read_text(encoding="utf-8")
            if not content.strip():
                issues.append(f"{rel_path}: empty file")
                continue

            # Syntax check
            try:
                py_compile.compile(str(full_path), cfile=cfile, doraise=True)
            except py_compile.PyCompileError as exc:
                issues.append(f"{rel_path}: syntax error - {exc}")
                continue

            # Secret patterns
            for pattern in SECRET_PATTERNS:
                if pattern.search(content):
                    issues.append(f"{rel_path}: possible secret/credential detected")
                    break

    if issues:
        return CheckResult(
            name="bad_patterns",
            passed=False,
            message=f"{len(issues)} issues found in changed files",
            details=issues,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name="bad_patterns",
        passed=True,
        message=f"Scanned {len(py_files)} Python files, no issues",
        duration_seconds=time.monotonic() - start,
    )


def check_policy_envelope(
    cwd: Path,
    base_branch: str,
    config: PolicyConfig,
) -> CheckResult:
    """R8.1: enforce the declarative ``[policy]`` envelope from artifacts.

    Reads the git diff and ``uv.lock`` only, never agent self-report.
    Fails CLOSED on any infrastructure error (diff unreadable, malformed
    policy) and on any envelope violation. Enforcement-machinery edits
    are a non-overridable halt. Violation details are packed as
    individual entries so ``VerificationResult.as_context()``'s
    ``details[:10]`` slice carries them into the retry prompt.
    """
    start = time.monotonic()
    # All three reads are strict: each is a SEPARATE git subprocess, so a
    # successful content read proves nothing about the two that follow.
    # A lenient read returns [] on timeout/nonzero exit, which the
    # evaluator cannot distinguish from "nothing changed" - the change
    # would then satisfy every path and size rule vacuously.
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        changed = git.get_diff_names(base_branch, cwd, strict=True)
        numstat = git.get_diff_numstat(base_branch, cwd, strict=True)
    except git.GitDiffError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message=(
                "policy envelope could not read the diff; failing closed "
                "(infrastructure error, not a policy pass)"
            ),
            details=[
                f"Error: {exc}",
                "The change cannot be proven within policy; do not treat "
                "this as permission to merge.",
            ],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    try:
        evaluation = evaluate_policy(changed, numstat, diff_text, config)
    except PolicyConfigError as exc:
        return CheckResult(
            name="policy_envelope",
            passed=False,
            message="policy envelope is misconfigured; failing closed",
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "policy",
                    f"policy envelope is misconfigured: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    # License gate (R8.1): resolve each newly-added uv.lock dependency's
    # license and classify it. Runs only when configured (license_allow
    # non-empty).
    violations = list(evaluation.violations) + _check_licenses(
        evaluation.new_dependencies,
        config,
    )
    blocking = [v for v in violations if v.blocking]
    advisories = [v for v in violations if not v.blocking]

    findings = [
        Finding.policy_violation(
            category=v.category,
            explanation=v.explanation,
            location=v.location,
            severity=v.severity,
            suggestion=v.suggestion,
        )
        for v in violations
    ]
    # Blocking violations first: as_context() slices details[:10] into the
    # retry prompt, and advisories must never crowd out a real failure.
    details = [v.explanation for v in blocking] + [v.explanation for v in advisories]

    if not blocking:
        message = evaluation.summary
        if advisories:
            message += f"; {len(advisories)} advisory(ies)"
        return CheckResult(
            name="policy_envelope",
            passed=True,
            message=message,
            details=details,
            findings=findings,
            duration_seconds=time.monotonic() - start,
        )
    message = f"{len(blocking)} policy violation(s)"
    if evaluation.machinery_hit:
        message += " including enforcement-machinery halt"
    return CheckResult(
        name="policy_envelope",
        passed=False,
        message=message,
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _check_licenses(
    new_dependencies: list[tuple[str, str]],
    config: PolicyConfig,
) -> list[PolicyViolation]:
    """Resolve + classify the licenses of newly-added dependencies.

    Denied (copyleft) and resolved-but-not-allowlisted licenses are
    blocking. A license that no source could resolve is governed by
    ``license_unresolved``: "block" (default, fail-closed - an unprovable
    dependency is not demonstrably inside the envelope) or "advisory".
    No-op when the gate is unconfigured or nothing new was added.
    """
    if not config.license_allow or not new_dependencies:
        return []
    uv_cache = licensing.uv_cache_dir()
    violations: list[PolicyViolation] = []
    for name, version in new_dependencies:
        resolved = licensing.resolve_license(
            name,
            version,
            uv_cache=uv_cache,
            use_pypi=config.license_use_network,
        )
        if resolved is None:
            advisory = config.license_unresolved == "advisory"
            source = (
                "uv cache + PyPI both missed"
                if config.license_use_network
                else "uv cache missed; network resolution disabled"
            )
            violations.append(
                PolicyViolation(
                    category="license_unresolved",
                    location=f"{name} {version}",
                    severity="advisory" if advisory else "high",
                    explanation=(
                        f"license could not be resolved for {name} {version} "
                        f"({source})" + ("; recorded as advisory" if advisory else "")
                    ),
                    suggestion=(
                        "Warm the uv cache (`uv sync`) or allow network "
                        "resolution; set [policy] license_unresolved = "
                        '"advisory" to accept unprovable licenses.'
                    ),
                )
            )
            continue
        verdict = classify_license(
            resolved,
            config.license_allow,
            config.license_deny_partial,
        )
        if verdict == "denied":
            violations.append(
                PolicyViolation(
                    category="license_denied",
                    location=f"{name} {version}",
                    explanation=(f"denied license '{resolved}' for dependency {name} {version}"),
                    suggestion="Drop the dependency or find a permissive alternative.",
                )
            )
        elif verdict == "unknown":
            violations.append(
                PolicyViolation(
                    category="license_not_allowed",
                    location=f"{name} {version}",
                    explanation=(
                        f"license '{resolved}' for {name} {version} is not in license_allow"
                    ),
                    suggestion=(
                        f"Add '{resolved}' to [policy] license_allow if it is "
                        "acceptable for this repo."
                    ),
                )
            )
    return violations


def check_test_adequacy(
    cwd: Path,
    base_branch: str,
    config: AdequacyConfig,
    autonomy_level: int = 0,
) -> CheckResult:
    """R8.5 Layer 0: did this change weaken the suite, and do its new
    tests assert anything falsifiable?

    Reads the diff and the changed test files only - no test execution,
    no coverage, no mutation tooling, no historical data. Fails CLOSED on
    an unreadable diff, like every other artifact-reading check here.

    Advisory unless the level (or an explicit opt-in) says otherwise:
    findings are recorded either way, so switching the gate on later
    starts from evidence rather than a guess.

    File STATUS is read alongside the names: the whole-file oracle floor
    is a rule about NEW test files, and applying it to a file someone
    merely edited would fail a one-line change for oracles that predate
    it. Diff discipline applies to every changed file regardless.
    """
    start = time.monotonic()
    try:
        diff_text = git.get_diff_content(base_branch, cwd)
        records = git.get_diff_name_status(base_branch, cwd, strict=True)
        changed = [path for _, path in records]
    except git.GitDiffError as exc:
        return CheckResult(
            name="test_adequacy",
            passed=False,
            message=(
                "test adequacy could not read the diff; failing closed "
                "(infrastructure error, not an adequacy pass)"
            ),
            details=[f"Error: {exc}"],
            findings=[
                Finding.infrastructure_error(
                    "adequacy",
                    f"adequacy could not read the diff: {exc}",
                )
            ],
            duration_seconds=time.monotonic() - start,
        )

    sources: dict[str, str] = {}
    for rel in changed:
        if not is_test_path(rel) or not rel.endswith(".py"):
            continue
        full = cwd / rel
        if not full.exists():
            continue  # deleted; the diff analysis covers it
        try:
            sources[rel] = full.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

    # Only status "A" is new content. A rename/copy destination ("R"/"C")
    # carries tests that already existed, so it is not held to the
    # new-file oracle floor.
    new_paths = {path for status, path in records if status.startswith("A") and path in sources}
    adequacy_findings = evaluate_layer0(
        diff_text,
        sources,
        config,
        new_paths=new_paths,
    )
    blocking = layer0_blocks(config, autonomy_level)
    severity = "high" if blocking else "advisory"
    findings = [
        Finding.adequacy_finding(
            category=str(f.kind),
            explanation=f.render(),
            location=f.path,
            severity=severity,
        )
        for f in adequacy_findings
    ]

    if not adequacy_findings:
        return CheckResult(
            name="test_adequacy",
            passed=True,
            message=(f"test adequacy: {len(sources)} changed test file(s), no weakening signals"),
            duration_seconds=time.monotonic() - start,
        )
    details = [f.render() for f in adequacy_findings]
    mode = "blocking" if blocking else "advisory"
    return CheckResult(
        name="test_adequacy",
        passed=not blocking,
        message=(f"{len(adequacy_findings)} test-adequacy finding(s) [{mode}]"),
        details=details,
        findings=findings,
        duration_seconds=time.monotonic() - start,
    )


def _count_before(word: str, text: str) -> int:
    """The integer immediately before the last ``word`` token in ``text``.

    mutmut reports "12 killed" / "3 survived" among prose, and the
    number is whatever sits in front of the word. Last occurrence wins,
    because ``mutmut results`` is read after ``mutmut run`` and the
    later figure is the settled one.

    Extracted from :func:`check_mutation_score` (#306 round 2) because
    the two copies of this loop, one per word, were most of that
    function's cyclomatic weight and left no room to tell a FAILED
    mutmut run apart from an empty one. A missing word yields 0, which
    is what "no such line" and "the line said zero" both mean here.
    """
    count = 0
    for line in text.splitlines():
        parts = line.lower().strip().split()
        for i, part in enumerate(parts):
            if part == word and i > 0:
                try:
                    count = int(parts[i - 1])
                except ValueError:
                    pass
    return count


def _changed_non_test_python(base_branch: str, cwd: Path) -> list[str]:
    """The non-test Python files in ``git diff <base>...HEAD``.

    One home for the rule, because two checks act on it and both report
    ``no_target`` when it comes back empty: the mutation gate mutates
    exactly these files and the dead-code scan scans exactly these
    files. Widening what counts as a test file in one place and not the
    other would make one of the two gaps say something the other does
    not mean.
    """
    changed = git.get_diff_names(base_branch, cwd)
    return [f for f in changed if f.endswith(".py") and not f.startswith("test")]


def check_mutation_score(
    cwd: Path,
    base_branch: str,
    threshold: float = 50.0,
    timeout: float = 600.0,
) -> CheckResult | NotMeasured:
    """Run mutation testing on changed files using mutmut.

    Only mutates Python files changed relative to base_branch. Returns a
    :class:`CheckResult` - PASS or FAIL against ``threshold`` - only when
    a score was actually measured. Every other path returns
    :class:`NotMeasured`, which is a SIDECAR record and never a row in
    ``checks`` (#306).

    Five paths return NotMeasured, and the ``reason`` token separates
    them because they are not the same event:

    - ``tool_missing``: mutmut is not on PATH. The operator asked for a
      measurement the machine cannot make.
    - ``no_target``: the diff changed no non-test Python file. Nothing
      to mutate; not a fault.
    - ``timed_out``: ``mutmut run`` exceeded ``timeout``.
    - ``command_failed``: mutmut ran, exited non-zero and produced no
      counts. Distinguished from the next by ``returncode`` alone,
      because a broken mutmut configuration and a clean run with nothing
      to do produce identical empty output.
    - ``no_mutants``: mutmut exited zero and generated no mutants.

    The sixth, ``read_only``, never reaches this function:
    :func:`_mutation_checks` owns it, because mutmut works by rewriting
    the source files it mutates.

    Every one of those five situations used to return
    ``CheckResult(passed=True)`` - the last two as a single path, since
    telling them apart is new here - so a green
    ``mutation_testing`` row meant "we did not look" as often as it
    meant "we looked and it was fine" - and
    :func:`kstrl.review.build_review_prompt` copied that row into the
    LLM reviewer's prompt as ``mutation_testing: PASS``.

    NotMeasured rather than a not-measured STATUS on the row, because
    ``passed`` is the only field every consumer reads: a third state
    there still reads as a pass through ``all(c.passed ...)``,
    ``report_lines``, ``ks sense --json`` and that reviewer prompt, for
    every reader not yet taught the new field. Absence from ``checks``
    is also the convention this repo already wrote down - see
    :func:`kstrl.feature_verify` on its own suppressed checks, "the
    suppressed checks are ABSENT from it rather than recorded as passing
    skips: a machine reader doing ``all(c.passed)`` must never see a
    check that measured nothing counted as a pass".

    And not a FAIL either, on the paths where something is genuinely
    wrong. A failing mechanical check is retry context for the engineer,
    and installing a binary is not a thing an engineer iteration can do,
    so a FAIL there spends ``repair_max_runs`` iterations on a diff that
    cannot change the outcome. Halting for a human on mutation infra
    failure is the L3+ behaviour in ``docs/dark-factory-roadmap.md``
    (Layer 2, "mutation infra failure halts for a human rather than
    skipping"), halt is not fail, and that layer is unbuilt. The sidecar
    is the third option: seen by the operator, gating nothing.
    """
    start = time.monotonic()

    if not shutil.which("mutmut"):
        return NotMeasured(
            MUTATION_TESTING_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "mutmut is not on PATH, so [verify] mutation_testing measured nothing",
        )

    py_files = _changed_non_test_python(base_branch, cwd)
    if not py_files:
        return NotMeasured(
            MUTATION_TESTING_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff changed no non-test Python file, so there was nothing to mutate",
        )

    # Run mutmut on changed files only
    paths_arg = " ".join(py_files)
    try:
        result = run_scrubbed(
            f"mutmut run --paths-to-mutate={paths_arg} --no-progress",
            cwd=cwd,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return NotMeasured(
            MUTATION_TESTING_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"mutmut run exceeded [verify] mutation_timeout of {timeout}s",
        )

    # Parse mutmut results
    try:
        results_proc = run_scrubbed("mutmut results", cwd=cwd, timeout=30)
        output = results_proc.stdout
    except subprocess.TimeoutExpired:
        output = result.stdout

    text = result.stdout + result.stderr + output
    killed = _count_before("killed", text)
    survived = _count_before("survived", text)

    total = killed + survived
    if total == 0:
        return _no_counts(result)

    score = (killed / total) * 100
    details = [
        f"Killed: {killed}, Survived: {survived}, Total: {total}",
        f"Score: {score:.1f}% (threshold: {threshold}%)",
    ]

    if score < threshold:
        return CheckResult(
            name=MUTATION_TESTING_CHECK,
            passed=False,
            message=f"Mutation score {score:.1f}% below threshold {threshold}%",
            details=details,
            duration_seconds=time.monotonic() - start,
        )

    return CheckResult(
        name=MUTATION_TESTING_CHECK,
        passed=True,
        message=f"Mutation score {score:.1f}% (threshold: {threshold}%)",
        details=details,
        duration_seconds=time.monotonic() - start,
    )


def _last_output_line(result: subprocess.CompletedProcess[str]) -> str:
    """The last line a failed tool printed, capped, for a gap's detail.

    stderr first because that is where a tool that could not start says
    so, and the last line because a traceback or a usage message puts
    the sentence a reader needs at the bottom.

    Capped for the reason git.py caps its stderr at 500: this reaches
    ``ks sense --json`` and the terminal, and one unbroken line of tool
    output has no bound.
    """
    tail = (result.stderr or result.stdout).strip().splitlines()
    return tail[-1][:500] if tail else "no output"


def _no_counts(result: subprocess.CompletedProcess[str]) -> NotMeasured:
    """Why mutmut produced no killed or survived count.

    Its own function so :func:`check_mutation_score` does not pay a
    branch for the distinction. Measured: inlining it takes that
    function from cognitive 9 to 14 against a gate of 15. The distinction is worth having: a
    broken ``[verify] mutation_command`` and a clean run over code with
    nothing mutable produce byte-identical empty output, and only
    ``returncode`` tells them apart. Reporting both as "no mutants" is
    how a permanently misconfigured mutation gate reads as a quiet,
    correct no-op.
    """
    if result.returncode != 0:
        return NotMeasured(
            MUTATION_TESTING_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"mutmut run exited {result.returncode} and reported no counts: "
            f"{_last_output_line(result)}",
        )
    return NotMeasured(
        MUTATION_TESTING_CHECK,
        NOT_MEASURED_NO_MUTANTS,
        "mutmut ran cleanly and generated no mutants, so there is no score",
    )


def _ruff_dead_code_command(read_only: bool) -> str:
    """The ruff invocation for the phase, in each of its two modes.

    ``--no-fix`` is explicit rather than implied by omitting ``--fix``: a
    project can set ``fix = true`` under ``[tool.ruff]``, which turns a
    bare ``ruff check`` into a fixing run. ``--no-cache`` so not even
    ``.ruff_cache`` appears in a tree kstrl was asked only to measure.
    """
    if read_only:
        return "ruff check --no-fix --no-cache --select F401,F811,F841 ."
    return "ruff check --fix --select F401,F811,F841 ."


def _ruff_count(output: str, *, read_only: bool) -> int:
    """How many findings ruff removed, or would remove.

    Two different numbers off two different lines, because the two modes
    print different things: a fixing run reports ``Found 3 errors (2
    fixed, 1 remaining).`` and a ``--no-fix`` run reports ``Found 3
    errors.`` with nothing removed. Last match wins in the fixing case,
    which is what the fused function did.
    """
    if read_only:
        found = re.search(r"Found (\d+) error", output)
        return int(found.group(1)) if found else 0
    count = 0
    for line in output.splitlines():
        if "fixed" in line.lower():
            match = re.search(r"(\d+)\s+fix", line.lower())
            if match:
                count = int(match.group(1))
    return count


def _commit_ruff_fixes(cwd: Path) -> None:
    """Stage and commit what ruff removed, so later checks see a clean tree.

    Everything EXCEPT the state directory (#274 review). Under
    ``use_worktrees=False`` this runs with ``cwd`` at the project root,
    so a bare ``git add -A`` commits kstrl's own live ``.kstrl/``
    journals onto the component branch the moment ruff fixes one
    finding - and ``check_diff_scope`` is deliberately un-carved, so the
    next pass fails on them and they ride into the PR. In a worktree the
    same exclusion is wanted for the opposite reason: a ``.kstrl/``
    there is the agent's, and this must not commit it on the agent's
    behalf.

    A list, not a string: ``run_scrubbed`` only shells out for a string,
    and the ``:(exclude)`` pathspec must reach git unmangled.

    A timeout here is swallowed rather than reported as a gap: the
    measurement already happened, and the row it produced is true. What
    is lost is the tidying, which the next check would notice for
    itself.
    """
    try:
        run_scrubbed(
            ["git", "add", "-A", "--", ".", f":(exclude){STATE_DIR_NAME}"],
            cwd=cwd,
            timeout=30,
        )
        run_scrubbed(
            'git commit -m "chore: auto-remove dead code (ruff F401/F811/F841)"',
            cwd=cwd,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        pass  # Non-fatal


def check_dead_code_ruff(
    cwd: Path,
    timeout: float = 300.0,
    *,
    read_only: bool = False,
) -> CheckResult | NotMeasured:
    """Auto-remove unused imports and locals with ruff F401/F811/F841.

    The first half of what ``check_dead_code`` used to do in one
    function, split out by #335 because one row covering two phases
    reported a pass for whichever of the two had not run.
    :func:`_dead_code_checks` records the full account.

    Always a row when ruff ran, because zero fixes is a measurement.
    Three paths return :class:`NotMeasured` instead, and the ``reason``
    token separates them:

    - ``tool_missing``: ruff is not on PATH.
    - ``timed_out``: the run exceeded ``timeout``.
    - ``command_failed``: ruff exited outside 0 (clean or fixed) and 1
      (findings). Measured on ruff 0.16.1, 2 is a configuration error;
      anything else is a tool that did not complete. This is the path
      that most needed splitting out - a bad ``ruff.toml`` printed no
      count, parsed to zero fixes, and read as a clean auto-fix phase.

    ``read_only=True`` (``ks sense``, R10.1) runs the SAME rule set with
    ``--no-fix`` and reports what the factory WOULD have removed instead
    of removing it. Nothing is edited, staged or committed. The factory
    owns the worktree it verifies, so editing and committing there is
    free; ``ks sense`` runs against the operator's live checkout, where
    a ``git add -A`` sweeps in every unrelated untracked file and the
    commit moves their HEAD.

    One divergence worth naming: the factory deletes the ruff-fixable
    subset before the detector in :func:`check_dead_code` looks, so that
    scan sees a cleaner tree than a read-only run does. A tree whose
    only dead code is ruff-fixable can therefore fail there and pass
    inside the factory. That is the tree being reported honestly, not a
    bug - but it is a difference.
    """
    start = time.monotonic()

    if not shutil.which("ruff"):
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "ruff is not on PATH, so no unused import or local was looked for",
        )

    try:
        result = run_scrubbed(_ruff_dead_code_command(read_only), cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"ruff exceeded [verify] subprocess_timeout of {timeout}s",
        )

    if result.returncode not in (0, 1):
        return NotMeasured(
            DEAD_CODE_RUFF_CHECK,
            NOT_MEASURED_COMMAND_FAILED,
            f"ruff check exited {result.returncode}: {_last_output_line(result)}",
        )

    count = _ruff_count(result.stdout + result.stderr, read_only=read_only)
    if read_only:
        message = f"ruff reports {count} auto-removable, not removed"
    else:
        message = f"ruff auto-fixed {count}"
        if count > 0:
            _commit_ruff_fixes(cwd)
    return CheckResult(
        name=DEAD_CODE_RUFF_CHECK,
        passed=True,
        message=message,
        duration_seconds=time.monotonic() - start,
    )


def _dead_code_command(
    cwd: Path,
    base_branch: str,
    command: str | None,
) -> str | NotMeasured:
    """What :func:`check_dead_code` should run, or why it cannot run.

    Its own function so the check does not pay a branch for a choice
    made before anything executes, and so the two reasons there is
    nothing to run are separated at the point where they are known
    rather than reconstructed later.

    A user-supplied ``command`` wins outright and is run as given: it is
    the operator's own program, in the same category as
    ``test_command``, and it replaces both vulture and the diff read
    that only exists to build vulture's argument list.
    """
    if command:
        return command
    if not shutil.which("vulture"):
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_TOOL_MISSING,
            "vulture is not on PATH and no [verify] dead_code_command is set, "
            "so nothing scanned for dead code",
        )
    py_files = _changed_non_test_python(base_branch, cwd)
    if not py_files:
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_NO_TARGET,
            "the diff changed no non-test Python file, so there was nothing to scan",
        )
    return f"vulture {' '.join(py_files)} --min-confidence 80"


def check_dead_code(
    cwd: Path,
    base_branch: str,
    command: str | None = None,
    timeout: float = 300.0,
) -> CheckResult | NotMeasured:
    """Detect dead code with vulture, or with the operator's own command.

    The second half of the old fused ``check_dead_code`` (#335). It
    scans for what ruff cannot see - unreachable functions, unused
    classes, unused attributes - over the non-test Python files in
    ``git diff <base>...HEAD``, and findings are reported as a FAIL for
    the engineer to fix on retry.

    Returns a row only when a scan happened. Four paths return
    :class:`NotMeasured`, and each ``reason`` token is a different
    event:

    - ``tool_missing``: no ``[verify] dead_code_command`` and no vulture
      on PATH.
    - ``no_target``: the diff changed no non-test Python file. Nothing
      to scan; not a fault.
    - ``timed_out``: the scan exceeded ``timeout``.
    - ``command_failed``: the detector exited non-zero and printed
      nothing. Measured on vulture 2.16: exit 3 is findings, 1 is
      invalid input and 2 is a bad command line, so a silent non-zero
      exit is the tool failing rather than a clean tree. Before the
      split it fell through to ``no remaining dead code``.

    All four used to be ``CheckResult(passed=True)``. A missing binary
    is not something the engineer's next diff can fix, so none of them
    is a FAIL either: a gap is seen by the operator and gates nothing.

    No ``read_only`` flag, unlike the ruff phase: vulture and an
    operator's own detector read the tree without changing it, so there
    is nothing narrower for this to do.
    """
    start = time.monotonic()

    detect_cmd = _dead_code_command(cwd, base_branch, command)
    if isinstance(detect_cmd, NotMeasured):
        return detect_cmd

    try:
        result = run_scrubbed(detect_cmd, cwd=cwd, timeout=timeout)
    except subprocess.TimeoutExpired:
        return NotMeasured(
            DEAD_CODE_CHECK,
            NOT_MEASURED_TIMED_OUT,
            f"the dead code scan exceeded [verify] subprocess_timeout of {timeout}s",
        )

    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if not output:
            return NotMeasured(
                DEAD_CODE_CHECK,
                NOT_MEASURED_COMMAND_FAILED,
                f"the dead code scan exited {result.returncode} and printed nothing",
            )
        # Filter out common false positives (e.g., __all__, __init__).
        real_issues = [
            line
            for line in output.splitlines()
            if line.strip() and not line.strip().startswith("#") and "__all__" not in line
        ]
        if real_issues:
            return CheckResult(
                name=DEAD_CODE_CHECK,
                passed=False,
                message=f"{len(real_issues)} dead code issues remaining",
                details=real_issues[:20],
                duration_seconds=time.monotonic() - start,
            )

    return CheckResult(
        name=DEAD_CODE_CHECK,
        passed=True,
        message="no remaining dead code",
        duration_seconds=time.monotonic() - start,
    )


def _dead_code_checks(
    cwd: Path,
    base_branch: str,
    config: VerifyConfig,
    *,
    read_only: bool,
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """``(rows, gaps)`` for the dead-code phases: at most one of each, twice.

    The same shape as :func:`_mutation_checks`, for the same reason
    (#306, #335): a check the operator TURNED OFF records nothing at
    all, and a check they turned on that measured nothing records why.
    One toggle, ``[verify] dead_code_cleanup``, still owns both phases -
    splitting the ROW is not splitting the switch.

    Order is load-bearing and is why this is a function rather than two
    calls inline: ruff runs FIRST so the detector scans a tree with the
    ruff-fixable subset already deleted. Reversing it changes what
    vulture reports.
    """
    if not config.dead_code_cleanup:
        return [], []
    ruff_outcome = check_dead_code_ruff(
        cwd,
        config.subprocess_timeout,
        read_only=read_only,
    )
    detect_outcome = check_dead_code(
        cwd,
        base_branch,
        config.dead_code_command,
        config.subprocess_timeout,
    )
    outcomes = (ruff_outcome, detect_outcome)
    return (
        [o for o in outcomes if isinstance(o, CheckResult)],
        [o for o in outcomes if isinstance(o, NotMeasured)],
    )


def _scope_checks(
    cwd: Path,
    base_branch: str,
    *,
    allowed_paths: list[str] | None,
    allowed_paths_error: str | None,
    harness_paths: list[str] | None,
    compare: bool,
) -> list[CheckResult]:
    """The scope checks Phase 1 appends, at most one of two.

    An unreadable scope source and an out-of-scope diff are alternatives
    rather than a check with a mode (#294), so the choice is made once,
    here, instead of inside a check that would then be named for the
    wrong one of them:

    - ``allowed_paths_error`` non-empty: ``scope_unreadable`` alone,
      UNGATED. The comparison is not merely turned off, it is
      unavailable - there is no trustworthy allowlist to compare
      against - so running ``check_diff_scope`` too would report a PASS
      ("no scope constraints") beside the refusal, which is the
      fail-open reading of the same state. The error wins even when a
      caller also supplies a list: a half-loaded state must not be
      judged on paths that may be stale.

      ``is not None``, not truthiness. Both review rounds hit this from
      opposite sides and both were right about the defect: truthiness
      lets an empty-string sentinel PASS a ``diff_scope`` that had no
      allowlist to compare, which is a fail-open in the one check whose
      job is to fail closed; ``is not None`` alone refused while naming
      no cause, rendering the bare "Error: ". Neither problem requires
      the other. This refuses on any non-None value and
      ``check_scope_unreadable`` substitutes
      :data:`NO_CAUSE_RECORDED` for the empty one, so an ambiguous
      sentinel is never read as permission and the refusal always says
      something. ``ComponentScope.resolve`` never produces "", but
      ``run_mechanical_verification`` is a public entry point.
    - otherwise ``diff_scope``, gated on ``compare``, which is
      ``[verify] check_diff_scope`` and nothing else. The one flag
      rather than the whole ``VerifyConfig``: this is the only field
      the decision reads, and the two ``list[str] | None`` arguments
      beside it are keyword-only so a transposition of the authored
      allowlist and the harness carve-out cannot type-check clean.

    Returns a list rather than taking the branch in
    ``run_mechanical_verification``: that function is already over the
    cyclomatic ratchet and is judged against its own previous value, so
    an ``if``/``elif`` there is a refusal at commit time.
    """
    if allowed_paths_error is not None:
        return [check_scope_unreadable(allowed_paths_error)]
    if compare:
        return [
            check_diff_scope(
                cwd,
                base_branch,
                allowed_paths,
                harness_paths=harness_paths,
            )
        ]
    return []


def _mutation_checks(
    cwd: Path,
    base_branch: str,
    config: VerifyConfig,
    *,
    read_only: bool,
) -> tuple[list[CheckResult], list[NotMeasured]]:
    """``(rows, gaps)`` for mutation testing: at most one of each (#306).

    Every reason mutation testing might not produce a score lives here
    or in :func:`check_mutation_score`, and all of them keep ``rows``
    empty. What differs is ``gaps``, and the difference is the point of
    round 2: a check the operator TURNED OFF records nothing, and a
    check they turned on that measured nothing records why.

    So an absent ``mutation_testing`` row plus an absent gap means
    disabled, and an absent row plus a gap means asked-for and not
    delivered. Seven states, one bit and one token to separate them,
    where before #306 six of the seven produced the same green row.

    ``read_only`` is the one reason that cannot live inside the check.
    mutmut works by rewriting the source files it mutates, so ``ks
    sense`` and :func:`run_undiffed_verification` must not call it at
    all rather than call it and have it decline: a flag threaded into a
    check just to be refused is a flag that can return a row, which is
    how the read-only skip came to report ``passed=True`` in the first
    place.

    A pair rather than mutating two lists the caller owns, and a
    function rather than two branches inline. Measured: inlining the
    toggle and the read-only test at the call site takes
    :func:`run_mechanical_verification` to cyclomatic 10 against a gate
    of 10 and cognitive 15 against a gate of 15.

    The two conditions decidable before anything runs - the toggle and
    ``read_only`` - are exactly the subject of #305 (one object owning
    every argument that decides whether a check can honestly run,
    alongside :data:`DIFF_DEPENDENT_CHECKS` and
    :func:`run_undiffed_verification`). The other four cannot join it:
    mutmut absent, timed out, failed and no mutants are only knowable
    after the check has run.
    """
    if not config.mutation_testing:
        return [], []
    if read_only:
        return [], [
            NotMeasured(
                MUTATION_TESTING_CHECK,
                NOT_MEASURED_READ_ONLY,
                "mutmut rewrites the files it mutates and cannot run read-only",
            )
        ]
    outcome = check_mutation_score(
        cwd,
        base_branch,
        config.mutation_threshold,
        config.mutation_timeout,
    )
    if isinstance(outcome, NotMeasured):
        return [], [outcome]
    return [outcome], []


#: Every check :func:`run_mechanical_verification` appends that answers
#: its question by reading ``git diff <base>...HEAD``.
#:
#: Beside the function that appends them, because a caller that has no
#: measurable base has to know which checks that rules out, and deriving
#: the list by reading this module's source is how two callers end up
#: disagreeing about it. ``mutation_testing`` belongs here even though
#: ``read_only=True`` already skips it: it mutates the files the diff
#: names, so with no diff there is nothing for it to mutate either.
DIFF_DEPENDENT_CHECKS: tuple[str, ...] = (
    "diff_scope",
    "bad_patterns",
    "policy_envelope",
    "test_adequacy",
    DEAD_CODE_CHECK,
    MUTATION_TESTING_CHECK,
)


def self_critique_progress_path(
    config: VerifyConfig,
    worktree_path: Path,
    prd_path: Path | None,
) -> Path | None:
    """The log ``check_self_critique`` would read, or None if it will not run.

    Read the log the engineer was actually pointed at: a factory
    component writes NEXT TO its PRD (the only location inside its
    allowedPaths), so resolving a repo-root default here would check a
    file that was never written and fail the component for the harness's
    own path confusion. An explicit config wins. ``prd_path`` is
    worktree-absolute at the factory call site, so the derived sibling is
    too; the join is a no-op for an absolute path and still anchors a
    relative one. With neither a PRD nor an explicit path there is no log
    to read, so the check is skipped rather than run against a path that
    cannot exist.

    Extracted (#288 review) because a caller has to be able to ask
    whether this check will run BEFORE the run, to say so: `ks feature`
    announces its report up front, and the announcement was silently
    wrong for an operator who had set ``require_self_critique``. Two
    copies of the rule is how the announcement and the run disagree, so
    there is one, and :func:`run_mechanical_verification` calls it too.
    """
    if not config.require_self_critique:
        return None
    if config.progress_file_path is not None:
        return worktree_path / Path(config.progress_file_path)
    if prd_path is not None:
        return worktree_path / component_progress_path(prd_path, None)
    return None


def run_undiffed_verification(
    worktree_path: Path,
    config: VerifyConfig,
) -> VerificationResult:
    """Mechanical verification over a tree with no base to diff against.

    The ONLY safe entry point for that case, and it is a function rather
    than a documented convention because :func:`narrow_to_undiffed`
    cannot deliver the guarantee its name promises (#288 review round
    2). Its ``replace`` reaches four of the six
    :data:`DIFF_DEPENDENT_CHECKS`; ``policy_envelope`` and
    ``test_adequacy`` are gated by ``policy_config`` and
    ``adequacy_config``, which are separate ARGUMENTS to
    :func:`run_mechanical_verification`, and ``allowed_paths_error``
    outranks the ``check_diff_scope`` toggle entirely because
    :func:`_scope_checks` reads it first and appends the ungated
    ``scope_unreadable`` on any non-None value. So a second caller
    writing ``config=narrow_to_undiffed(cfg), policy_config=pc`` gets
    ``policy_envelope`` reporting a PASS over an empty diff: the exact
    defect the narrowing is named for, reintroduced by an argument the
    narrowing cannot see.

    This owns all of them. There is no parameter here for anything that
    consumes a diff, so the four suppressed by config and the three
    suppressed by argument are suppressed the same way: by not being
    reachable. ``read_only=True`` for the same reason ``ks sense`` uses
    it (R10.1) - the two checks that would rewrite the tree they measure
    are forbidden.

    ``base_branch=""`` is the honest value for "there is no base here"
    and is never read, because nothing left running consumes one.
    ``prd_path=None`` skips the PRD-derived checks: ``prd_stories``
    re-reads a flag the agent itself set, which is a self-report rather
    than an independent measurement.

    The structural version of this - one object owning every argument
    that decides whether a check can honestly run - is tracked on #305.
    """
    return run_mechanical_verification(
        worktree_path=worktree_path,
        prd_path=None,
        base_branch="",
        allowed_paths=None,
        allowed_paths_error=None,
        config=narrow_to_undiffed(config),
        read_only=True,
    )


def narrow_to_undiffed(config: VerifyConfig) -> VerifyConfig:
    """``config`` with every :data:`DIFF_DEPENDENT_CHECKS` toggle off.

    Prefer :func:`run_undiffed_verification`, which owns the arguments
    this cannot reach. Exported on its own only because the announcement
    side of a report needs the narrowed config to say what will run.

    For a caller whose tree has no base it can honestly diff against -
    `ks feature` (#288), where nothing commits for the agent and the
    branch the loop checks out may BE the base branch, so
    ``base...HEAD`` is routinely empty and a diff-based check would
    report ``0 files, all within scope`` over work it never saw.

    An empty diff is indistinguishable from nothing changed: the lenient
    git helpers return an empty file list either way, and even
    ``get_diff_names(..., strict=True)`` returns ``[]`` without raising.
    So the only honest answer is not to run those checks, which is what
    this does.

    Note what it does NOT cover, because the toggles cannot. ``policy``
    and ``adequacy`` are separate config objects and are suppressed by
    not being passed at all. And ``allowed_paths_error`` outranks
    ``check_diff_scope`` entirely: :func:`_scope_checks` reads it first
    and, on ANY non-None value, appends :func:`check_scope_unreadable`
    instead, which is ungated by this config and fails closed by design
    (#294). So a caller relying on this narrowing must still leave that
    argument None, but for the opposite reason to the one that held
    before #294: the risk is no longer a ``diff_scope`` PASS over a diff
    it never saw, it is a hard scope_unreadable FAIL over a scope the
    caller never had.
    """
    return replace(
        config,
        check_diff_scope=False,
        check_bad_patterns=False,
        dead_code_cleanup=False,
        mutation_testing=False,
    )


class MechanicalVerification(Protocol):
    """The call shape of :func:`run_mechanical_verification` (#316).

    ``PipelineHooks.run_mechanical_verification`` was typed
    ``Callable[..., VerificationResult]``, and ``...`` means mypy checks
    NOTHING about the arguments - which matters because that hook is how
    the only call site carrying a real component's scope reaches the
    function. Measured on this branch: with the hook typed ``...``,
    swapping ``harness_paths=scope.harness_paths`` for
    ``harness_paths=scope.error`` - a ``str | None`` into a
    ``list[str] | None`` slot, an authored carve-out replaced by the
    snapshot's failure to read one - left ``mypy --strict`` reporting
    SUCCESS. With this Protocol the same swap is
    ``error: Argument "harness_paths" to "__call__" of
    "MechanicalVerification" has incompatible type "str | None";
    expected "list[str] | None"``.

    Making the arguments keyword-only stops a SLOT from being inherited
    silently; it cannot stop a wrong value being handed to the right
    name. Only a type can, and only if there is one.

    The defaults below are spelled as real values rather than the
    conventional ``= ...`` so that ``inspect.Signature`` equality can
    compare this to the function in one assertion; a Protocol that has
    drifted is worse than none, because it would type-check calls the
    function rejects. See
    ``test_the_protocol_says_exactly_what_the_function_says``.
    """

    def __call__(
        self,
        worktree_path: Path,
        prd_path: Path | None,
        base_branch: str,
        allowed_paths: list[str] | None,
        config: VerifyConfig,
        *,
        allowed_paths_error: str | None = None,
        harness_paths: list[str] | None = None,
        pre_run_prd_path: Path | None = None,
        fixtures_config: FixturesConfig | None = None,
        policy_config: PolicyConfig | None = None,
        adequacy_config: AdequacyConfig | None = None,
        autonomy_level: int = 0,
        component_id: str | None = None,
        read_only: bool = False,
    ) -> VerificationResult: ...


def run_mechanical_verification(
    worktree_path: Path,
    prd_path: Path | None,
    base_branch: str,
    allowed_paths: list[str] | None,
    config: VerifyConfig,
    *,
    allowed_paths_error: str | None = None,
    harness_paths: list[str] | None = None,
    pre_run_prd_path: Path | None = None,
    fixtures_config: FixturesConfig | None = None,
    policy_config: PolicyConfig | None = None,
    adequacy_config: AdequacyConfig | None = None,
    autonomy_level: int = 0,
    component_id: str | None = None,
    read_only: bool = False,
) -> VerificationResult:
    """Run all mechanical checks. All checks run even if earlier ones fail.

    Everything after ``config`` is keyword-only (#316), so an inserted
    parameter cannot shift a later argument into a slot that means
    something else - and three of the arguments here mean opposite
    things in near-identical types (see :class:`MechanicalVerification`,
    which covers the half that keyword-only does not). Cost: none. No
    caller passed any of them positionally.

    ``prd_path=None`` (R10.1, ``ks sense``) skips the PRD-dependent
    checks: ``prd_stories``, the approved-fixtures oracle (fixtures are
    declared in the PRD), and ``self_critique`` unless
    ``config.progress_file_path`` names the log explicitly (with no PRD
    there is no sibling to derive it from). Every other check runs
    exactly as it does with a real path.

    ``harness_paths`` (#264) is the per-component carve-out for kstrl's
    OWN files, forwarded to ``check_diff_scope``. It reaches the factory
    from the run's plan-time scope snapshot (``scope.RunScope``), which
    is also where ``allowed_paths`` comes from; ``ks sense`` leaves both
    None because it judges an operator's diff, not a factory
    component's.

    ``allowed_paths_error`` (#269) is that snapshot reporting that it
    could not read the component's scope at all. It replaces the
    ``diff_scope`` comparison with ``scope_unreadable``, an ungated
    fail-closed refusal named for its own cause (#294) - see
    ``_scope_checks``. Any non-None value refuses, empty included. ``ks sense`` never sets it: it
    has no plan-time snapshot, so its scope is whatever
    ``--allowed-paths`` gave it.

    ``pre_run_prd_path`` (#269) is the copy of ``prd_path`` the run
    started with, forwarded to ``check_prd_stories``, which fails closed
    on a PRD the component rewrote. Also None for ``ks sense``: there is
    no pre-run copy to compare an operator's working tree against.

    ``fixtures_config`` (R7.2): when provided AND ``.enabled`` is true,
    the approved-fixtures oracle runs against the PRD's ``fixtures``
    entries - sandboxed subprocess execution lives in
    ``kstrl.fixtures``. ``component_id`` keys the fixture snapshot
    used for regression detection; None disables snapshotting only.

    ``read_only=True`` (``ks sense``, R10.1) forbids the two checks that
    change the tree they measure: ``dead_code_ruff`` drops its auto-fix
    and the ``git add -A`` / ``git commit`` that followed it, and
    ``mutation_testing`` is not run at all. What remains still shells
    out to the project's OWN configured test / typecheck / lint (and
    fixture) commands, which are the operator's programs and write their
    own caches; kstrl suppresses only kstrl's writes.

    ``mutation_testing``, ``dead_code_ruff`` and ``dead_code`` append NO
    ROW rather than a passing one whenever nothing was measured (#306,
    #335). See :func:`_mutation_checks` and :func:`_dead_code_checks`. A
    consumer reading ``checks`` must already tolerate those rows'
    absence, because ``[verify] mutation_testing`` and ``[verify]
    dead_code_cleanup`` both default to false; what changed is that
    absence is now the ONLY thing a non-measurement can look like.

    ``[verify] dead_code_cleanup`` produces TWO rows, not one: the ruff
    F401/F811/F841 phase and the vulture-or-``dead_code_command`` phase
    answer for themselves, because one row for both reported a pass for
    a scan that never ran (#335).

    :attr:`VerificationResult.not_measured` is where the reason goes,
    and it is the half that makes the absence readable rather than
    merely honest. See :class:`NotMeasured`.
    """
    checks: list[CheckResult] = []
    not_measured: list[NotMeasured] = []

    if prd_path is not None:
        checks.append(check_prd_stories(prd_path, pre_run_prd_path))

    checks.append(
        check_test_suite(
            worktree_path,
            config.test_command,
            config.subprocess_timeout,
            config.test_tool,
        )
    )

    checks.append(
        check_typecheck(
            worktree_path,
            config.typecheck_command,
            config.subprocess_timeout,
            config.typecheck_tool,
        )
    )

    checks.append(
        check_linter(
            worktree_path,
            config.lint_command,
            config.subprocess_timeout,
            config.lint_tool,
        )
    )

    checks.extend(
        _scope_checks(
            worktree_path,
            base_branch,
            allowed_paths=allowed_paths,
            allowed_paths_error=allowed_paths_error,
            harness_paths=harness_paths,
            compare=config.check_diff_scope,
        )
    )

    if config.check_bad_patterns:
        checks.append(check_bad_patterns(worktree_path, base_branch))

    # R8.1 policy envelope: opt-in ([policy] enabled). When disabled the
    # check is not appended, so existing runs are unchanged.
    if policy_config is not None and policy_config.enabled:
        checks.append(
            check_policy_envelope(
                worktree_path,
                base_branch,
                policy_config,
            )
        )

    # R8.5 Layer 0: opt-in ([adequacy] enabled), advisory unless the
    # level or config says block. Runs before the expensive layers so a
    # suite-weakening diff is reported even when mutation is off.
    if adequacy_config is not None and adequacy_config.enabled:
        checks.append(
            check_test_adequacy(
                worktree_path,
                base_branch,
                adequacy_config,
                autonomy_level,
            )
        )

    dead_code_rows, dead_code_gaps = _dead_code_checks(
        worktree_path,
        base_branch,
        config,
        read_only=read_only,
    )
    checks.extend(dead_code_rows)
    not_measured.extend(dead_code_gaps)

    mutation_rows, mutation_gaps = _mutation_checks(
        worktree_path,
        base_branch,
        config,
        read_only=read_only,
    )
    checks.extend(mutation_rows)
    not_measured.extend(mutation_gaps)

    progress_path = self_critique_progress_path(config, worktree_path, prd_path)
    if progress_path is not None:
        checks.append(
            check_self_critique(
                progress_path,
                config.self_critique_min_bullets,
            )
        )

    if prd_path is not None and fixtures_config is not None and fixtures_config.enabled:
        # Imported lazily: fixtures.py imports CheckResult/run_scrubbed
        # from this module, so a module-level import would be a cycle.
        from kstrl.fixtures import check_fixtures_from_prd

        checks.append(
            check_fixtures_from_prd(
                prd_path,
                worktree_path,
                fixtures_config,
                component_id=component_id,
            )
        )

    # ``checks`` only: a check that measured nothing neither passes nor
    # fails the run (#306).
    passed = all(c.passed for c in checks)
    return VerificationResult(passed=passed, checks=checks, not_measured=not_measured)
