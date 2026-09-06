"""#286: what the scaffold ledger says about a prompt file on disk.

`ks init` writes ``scripts/kstrl/prompt.md`` through
``_create_if_missing``, which by design never overwrites, and
``run_loop`` prefers that file over ``DEFAULT_PROMPT``. Before this,
``DEFAULT_PROMPT_VERSION`` was read nowhere in ``kstrl/``, so an H3
version bump reached greenfield inits and the missing-file fallback and
no already-initialised project at all.

The mechanism keys on the file's SHA-256 against a ledger of every body
the harness has ever shipped, so it classifies files written long before
the mechanism existed. Two kinds of test live here:

- **Real-ledger tests** use ``SCAFFOLDED_TEMPLATES`` as shipped. They
  cover the current / unrecognised / absent paths and the ledger's own
  invariants.
- **Synthetic-ledger tests** monkeypatch in a two-row template built
  from literal bodies. Producing a genuinely stale file against the real
  ledger would mean shipping a 4KB historical prompt body as a fixture,
  or reading it back out of git history, which a shallow CI checkout
  does not have. The classifier is indifferent to which ledger it reads,
  so the synthetic one exercises the same code path with bodies the test
  can state outright.

The 1.1.1 row was validated during development against a real
installation: the owner's writers-room project carries a prompt.md whose
digest is exactly that row.

What kstrl DOES about a stale file - the `ks init` report, the upgrade
and its guards, and the operator-facing surfaces - is in
tests/test_prompt_upgrade.py, which imports the fixtures below.
"""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import pytest

from kstrl import init_cmd
from kstrl.init_cmd import (
    DEFAULT_PROMPT,
    DEFAULT_PROMPT_VERSION,
    SCAFFOLDED_TEMPLATES,
    ScaffoldedTemplate,
    classify_scaffold,
    classify_scaffolded_path,
    staleness_notice,
)
from tests.helpers import astwalk
from tests.test_init_cmd import run_init_capturing

OLD_BODY = "# old engineer instructions\n"
NEW_BODY = "# new engineer instructions\n"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SYNTHETIC = ScaffoldedTemplate(
    filename="prompt.md",
    constant_name="DEFAULT_PROMPT",
    body=NEW_BODY,
    history=(
        (_sha256(OLD_BODY), "9.0.0"),
        (_sha256(NEW_BODY), "9.1.0"),
    ),
)


@pytest.fixture
def synthetic_ledger(monkeypatch: pytest.MonkeyPatch) -> ScaffoldedTemplate:
    """Swap the shipped ledger for a two-row one built from literals."""
    monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (SYNTHETIC,))
    return SYNTHETIC


def _prompt_at(root: Path, body: str) -> Path:
    path = root / "scripts" / "kstrl" / "prompt.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    return path


class TestClassification:
    def test_stale_file_is_recognised_as_the_body_it_came_from(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        state = classify_scaffolded_path(_prompt_at(tmp_path, OLD_BODY))
        assert state is not None
        assert state.status == "stale"
        assert state.shipped_label == "9.0.0"

    def test_current_file_is_not_stale(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        state = classify_scaffolded_path(_prompt_at(tmp_path, NEW_BODY))
        assert state is not None
        assert state.status == "current"

    def test_current_file_is_not_stale_against_the_real_ledger(self, tmp_path: Path) -> None:
        """The bytes `ks init` writes today must classify as current, or
        every freshly-initialised project would be warned at once."""
        state = classify_scaffolded_path(_prompt_at(tmp_path, DEFAULT_PROMPT))
        assert state is not None
        assert state.status == "current"
        assert state.shipped_label == DEFAULT_PROMPT_VERSION

    def test_a_body_kstrl_never_shipped_is_unrecognised(self, tmp_path: Path) -> None:
        """The stand-in for "no version stamp": nothing in the file says
        where it came from, and its digest matches nothing we shipped.
        An edited prompt and a prompt from a build outside this history
        are indistinguishable, so neither is claimed to be stale."""
        state = classify_scaffolded_path(_prompt_at(tmp_path, DEFAULT_PROMPT + "\nmy own rule\n"))
        assert state is not None
        assert state.status == "unrecognised"
        assert state.shipped_label is None

    def test_missing_file_is_absent(self, tmp_path: Path) -> None:
        state = classify_scaffolded_path(tmp_path / "scripts" / "kstrl" / "prompt.md")
        assert state is not None
        assert state.status == "absent"

    def test_unreadable_file_makes_no_claim(self, tmp_path: Path) -> None:
        path = tmp_path / "prompt.md"
        path.write_bytes(b"\xff\xfe not utf-8")
        state = classify_scaffolded_path(path)
        assert state is not None
        assert state.status == "unrecognised"

    def test_a_file_kstrl_does_not_scaffold_is_not_classified(self, tmp_path: Path) -> None:
        """``--prompt`` and ``PROMPT_FILE`` can point anywhere; a file
        the operator named themselves is not a scaffold to speak about."""
        path = tmp_path / "my_prompt.md"
        path.write_text(DEFAULT_PROMPT)
        assert classify_scaffolded_path(path) is None

    def test_classify_scaffold_covers_every_template(self, tmp_path: Path) -> None:
        states = classify_scaffold(tmp_path)
        assert [s.template.filename for s in states] == [t.filename for t in SCAFFOLDED_TEMPLATES]
        assert {s.status for s in states} == {"absent"}


class TestWarningText:
    def test_stale_file_warns_and_names_both_versions(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        notice = staleness_notice(_prompt_at(tmp_path, OLD_BODY))
        assert notice is not None
        assert "9.0.0" in notice.headline
        assert "9.1.0" in notice.headline
        assert "every change to that template since 9.0.0" in notice.advice
        assert "ks init --upgrade-prompts" in notice.advice

    def test_current_file_says_nothing(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        assert staleness_notice(_prompt_at(tmp_path, NEW_BODY)) is None

    def test_edited_file_says_nothing(self, tmp_path: Path) -> None:
        """A warning on a prompt somebody customised on purpose is noise,
        and noise gets ignored."""
        assert staleness_notice(_prompt_at(tmp_path, DEFAULT_PROMPT + "\nmine\n")) is None

    def test_missing_file_says_nothing(self, tmp_path: Path) -> None:
        """``run_loop`` already announces its own DEFAULT_PROMPT fallback."""
        assert staleness_notice(tmp_path / "scripts" / "kstrl" / "prompt.md") is None

    def test_a_relocated_prompt_is_not_told_to_run_a_no_op(
        self, tmp_path: Path, synthetic_ledger: ScaffoldedTemplate
    ) -> None:
        """`ks init` only ever scaffolds and upgrades under
        scripts/kstrl/, so naming --upgrade-prompts to somebody whose
        [paths] prompt lives elsewhere would be advice that silently does
        nothing to their file."""
        path = tmp_path / "prompts" / "prompt.md"
        path.parent.mkdir(parents=True)
        path.write_text(OLD_BODY)
        notice = staleness_notice(path)
        assert notice is not None
        assert "9.0.0" in notice.headline
        assert "not the copy under scripts/kstrl/" in notice.advice
        assert "Run `ks init --upgrade-prompts`" not in notice.advice

    def test_the_oldest_body_names_itself_not_the_one_above_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Several rows back, the message must still name the row the
        file actually matches, not merely the previous one."""
        older = "# older\n"
        three_rows = ScaffoldedTemplate(
            filename="prompt.md",
            constant_name="DEFAULT_PROMPT",
            body=NEW_BODY,
            history=(
                (_sha256(older), "8.0.0"),
                (_sha256(OLD_BODY), "9.0.0"),
                (_sha256(NEW_BODY), "9.1.0"),
            ),
        )
        monkeypatch.setattr(init_cmd, "SCAFFOLDED_TEMPLATES", (three_rows,))
        notice = staleness_notice(_prompt_at(tmp_path, older))
        assert notice is not None
        assert "shipped at 8.0.0" in notice.headline
        assert "this kstrl ships 9.1.0" in notice.headline
        assert "since 8.0.0" in notice.advice


# The ledger exactly as this PR records it. Its whole value is the OLD
# rows: they are the only thing that can recognise a body already on an
# operator's disk. So the snapshot pins the prefix and the test below
# permits growth and nothing else, which is what makes "append only" a
# mechanism rather than a sentence in a comment.
_RECORDED_HISTORY: dict[str, tuple[tuple[str, str], ...]] = {
    "prompt.md": (
        (
            "15810563f3843b6634f6207d052710d72aa4fda0aa32ac86aa7718de86d34140",
            "pre-1.0.0 (2026-01-15)",
        ),
        (
            "9eb6d8f4c956d6fcacf2f39eed4a696e2755ce2ae0e24f53d08e98227fc37fc3",
            "pre-1.0.0 (2026-01-28)",
        ),
        (
            "5ec3e510a0dbd6ff41b181259b707f33a715715648cee0607ae5db6cf9992046",
            "pre-1.0.0 (2026-05-27)",
        ),
        ("a4a3a090139c370d7eecd12e3ef98055352110722750bb7b4cbf9bc50b1b9125", "1.0.0"),
        ("aa7fa6acb045dc6105d1a4c4ce8b687e1e04289c7b751eb0373b7c59dca3f7ae", "1.1.0"),
        # The body #276 measured a wasted engineer iteration on, at
        # roughly $4. Recognising it on disk is why the ledger exists.
        ("4f7370f5f4efb2d9b89ce6ae09fcbf7e5c3c8fb3db22cdeb07a9221ccbc638dc", "1.1.1"),
        ("9bde9b20785f3740396906d1d199c2228c553c11ae956dc2f85d8aa2439fb49b", "1.2.0"),
        ("392eb698daf71d486a9d4573698df3bb2b3ca4be87c178657accc8a66c54f384", "1.3.0"),
    ),
    "understand_prompt.md": (
        ("5514376b0beeb484755d2d7d5effbe9a749b2d0972ddd30e7911e47bcf73e4ff", "2026-01-14"),
        ("1e700b55db8316392de146c549ef9fe9acf503af5c6ba2780f9d341728ac39c4", "2026-01-15"),
        ("fd02d9e3f2e559db5625c4db2d81ef0d24df481a4f4d4f5506fddd9b0962c53a", "2026-07-20"),
        ("cfd43bfeb80eaaf559ccb32d993fc2c5b2471ff90c7816648743135c2aa29688", "2026-07-21"),
    ),
    "feature_understand_prompt.md": (
        ("5096447a6228e93d7d824ff5e1a334ef3eaf9edc9314a3fb7c6f7f04936cf06f", "2026-01-28"),
        ("e05fedd0ea1aff624966f4ee1e572c1af6f3926dd1b38b64678fdd6525a6f31a", "2026-07-20"),
        ("eb3637acf1918da23e27ad3f4d30bab32b1edd797b4bd1b5587b82b656affb09", "2026-07-21"),
    ),
}


# --- what `run_init` actually scaffolds, in two layers -------------------
#
# LAYER 1, :func:`_names_a_template`, counts every expression in
# ``kstrl/`` that folds to a template filename, per module. It is the
# net: a file cannot be written under a name the package never spells,
# so a fourth template has to appear here first, whatever shape writes
# it. It resolves nothing and enumerates no node types.
#
# LAYER 2, :func:`_scaffolded_in`, pairs each filename with the constant
# whose body goes into it, which is the half the ledger is keyed on and
# the half a count cannot say. #324 records eleven guards each
# re-implementing the resolution that needs; this one asks
# ``tests/helpers/astwalk.py`` for it instead.

#: What every scaffolded prompt template's filename ends with. Suffix
#: rather than equality so that ``scripts/kstrl/prompt.md`` folds too:
#: four of the six modules below spell the path, not the bare name.
_TEMPLATE_SUFFIX = "prompt.md"

#: The one function `ks init` writes a scaffolded file through.
_SCAFFOLD_WRITER = "_create_if_missing"

#: Every module in ``kstrl/`` that spells a template filename, and how
#: many times. Adding a row is not forbidden, it is the point: the diff
#: that adds one is where somebody says which template it is and why the
#: ledger does or does not need it.
#:
#: ``init_cmd.py``'s six are the three ledger rows and the three
#: ``_create_if_missing`` calls. The rest are surfaces that point an
#: operator at a file: the CLI's messages and options, the config's
#: prompt-path default, the wizard's preview and ``launch``'s resolution
#: of the engineer prompt.
#:
#: #229 took ``config.py`` from four to one and dropped
#: ``config_report.py`` entirely. Neither is a template going quiet: the
#: three extra config.py spellings and the config_report.py one were
#: ``root_dir / "scripts/kstrl/prompt.md"`` copied into ``from_env``,
#: ``from_toml``, ``load`` and ``kstrl_config_defaults``, and all four
#: now go through ``KstrlConfig.anchored``, which anchors the FIELD
#: default. The one remaining spelling is that field default, so the net
#: still sees the filename in the module that owns it.
EXPECTED_TEMPLATE_FILENAMES: dict[str, int] = {
    "cli.py": 8,
    "config.py": 1,
    "init_cmd.py": 6,
    "init_wizard.py": 3,
    "launch.py": 1,
}


def _names_a_template(node: ast.AST) -> bool:
    """Does this expression fold to a scaffolded template's filename?

    Layer 1's whole predicate. It names no node type and no field, so a
    filename assembled with ``+``, held in a table or built into an
    f-string counts exactly like one written at the call site. What
    folding cannot decide is a name the INTERPRETER has to build, and
    ``test_a_filename_the_interpreter_builds_is_a_known_miss`` pins that
    residual rather than implying it away.
    """
    folded = astwalk.folded_str(node)
    return folded is not None and folded.endswith(_TEMPLATE_SUFFIX)


def _scaffolded_in(tree: ast.Module) -> set[tuple[str, str]]:
    """``(filename, constant_name)`` for every template this module writes.

    The same discipline H3 applies one level up with
    ``test_no_unenrolled_prompt_constants``: without it, the next
    ``_create_if_missing(kstrl_dir / "x_prompt.md", DEFAULT_X_PROMPT,
    ui)`` would be un-ledgered, un-warned and un-upgradable, and nothing
    would fail. Reading the source rather than the ledger is the point,
    because the ledger is the thing under test.

    Three shapes round 1 could not see, all closed by
    ``tests/helpers/astwalk.py`` rather than by another private copy of
    its resolution: the writer reached through an import alias or a
    module (``astwalk.bindings``), a filename assembled from pieces
    (``astwalk.folded_str``), and a body reached as an attribute
    (``astwalk.leaf_name``).

    ``module`` is ``init_cmd``'s own dotted name whatever tree is walked,
    because it is read only to resolve a RELATIVE import, and the probes
    below are snippets standing in for that one module's source.
    """
    table = astwalk.bindings(tree, module=astwalk.module_name(Path(init_cmd.__file__)))
    return {
        pair
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _writes_a_scaffold(node, table)
        for pair in _scaffold_pair(node)
    }


def _writes_a_scaffold(node: ast.Call, table: astwalk.Bindings) -> bool:
    """Is this call ``_create_if_missing``, however the callee is spelled?

    The bare name, ``init_cmd._create_if_missing``, and the import alias
    ``from kstrl.init_cmd import _create_if_missing as _cim``, which the
    resolver decides and a name match cannot. A LOCAL rebind of the
    function object stays a miss; layer 1 is what still counts the
    filename such a call writes.
    """
    origin = table.resolve(node.func)
    return astwalk.leaf_name(node.func) == _SCAFFOLD_WRITER or (
        origin is not None and origin.endswith(f".{_SCAFFOLD_WRITER}")
    )


def _scaffold_pair(node: ast.Call) -> list[tuple[str, str]]:
    """The ``(filename, constant)`` one write names, if it names both."""
    if len(node.args) < 2:
        return []
    target, content = node.args[0], node.args[1]
    filename = astwalk.folded_str(target) or (
        astwalk.folded_str(target.right) if isinstance(target, ast.BinOp) else None
    )
    constant = astwalk.leaf_name(content)
    if filename is None or constant is None or not constant.endswith("_PROMPT"):
        return []
    return [(filename, constant)]


class TestLedgerIntegrity:
    """The ledger is the mechanism. If it can drift, there is no plan."""

    def test_every_live_body_is_the_newest_row(self) -> None:
        for template in SCAFFOLDED_TEMPLATES:
            digest, _label = template.history[-1]
            assert _sha256(template.body) == digest, (
                f"{template.constant_name} changed without a new row in "
                f"SCAFFOLDED_TEMPLATES (kstrl/init_cmd.py). APPEND "
                f"({_sha256(template.body)!r}, <label>) to its history; "
                "never edit or drop an older row, because an old row is "
                "the only thing that can recognise a copy already on "
                "someone's disk."
            )

    def test_recorded_rows_are_never_edited_or_dropped(self) -> None:
        """Append-only, enforced rather than asked for."""
        assert {t.filename for t in SCAFFOLDED_TEMPLATES} == set(_RECORDED_HISTORY)
        for template in SCAFFOLDED_TEMPLATES:
            recorded = _RECORDED_HISTORY[template.filename]
            assert template.history[: len(recorded)] == recorded, (
                f"{template.constant_name}'s recorded history changed. A "
                "new body is an APPEND: add a row at the end here and in "
                "kstrl/init_cmd.py, and leave every earlier row exactly "
                "as it is. Editing or dropping one destroys the only "
                "record that can recognise a copy already on an "
                "operator's disk."
            )

    def test_history_rows_are_unique_and_non_empty(self) -> None:
        for template in SCAFFOLDED_TEMPLATES:
            digests = [row[0] for row in template.history]
            assert digests, f"{template.constant_name} has no history"
            assert len(set(digests)) == len(digests)
            labels = [row[1] for row in template.history]
            assert len(set(labels)) == len(labels)

    def test_every_filename_a_template_is_written_under_is_pinned(self) -> None:
        """Layer 1, the net: pin every spelling of a template's filename.

        A file cannot be written under a name the package never spells,
        so a fourth template has to change this dict whatever shape
        writes it: a loop over a table, a name built with ``+``, a
        writer that is not ``_create_if_missing`` at all. That is why
        this layer resolves nothing and enumerates no node types.

        It is also what closes the one shape layer 2 discloses, a writer
        reached through a local rebind: the filename is still spelled at
        the call, so the count still moves.
        """
        astwalk.assert_census(
            sources=astwalk.package_sources(),
            sees=_names_a_template,
            expected=EXPECTED_TEMPLATE_FILENAMES,
            control='_create_if_missing(kstrl_dir / ("x" + "_prompt.md"), BODY, ui)\n',
            message=(
                "The set of places that name a scaffolded prompt template changed. "
                "If this is a new template, enrol it in SCAFFOLDED_TEMPLATES with "
                "its shipped history; an un-enrolled template reproduces #286 for "
                "itself. If it is another surface pointing an operator at a file, "
                "add the row with a reason."
            ),
        )

    def test_every_template_init_scaffolds_is_enrolled(self) -> None:
        """Layer 2, the message: name the filename and the constant."""
        enrolled = {(t.filename, t.constant_name) for t in SCAFFOLDED_TEMPLATES}
        assert _scaffolded_in(astwalk.parsed(Path(init_cmd.__file__))) == enrolled, (
            "run_init and SCAFFOLDED_TEMPLATES disagree about which "
            "prompt templates exist. Add the new one to the ledger with "
            "its shipped history, or fix the filename/constant pairing; "
            "an un-enrolled template reproduces #286 for itself."
        )

    def test_filenames_match_what_init_scaffolds(self, tmp_path: Path) -> None:
        run_init_capturing(tmp_path)
        for template in SCAFFOLDED_TEMPLATES:
            assert (tmp_path / "scripts" / "kstrl" / template.filename).exists()


class TestTheScaffoldWalkCatchesWhatItClaims:
    """Layer 2's reach, measured rather than asserted in a docstring.

    Round 1 had no positive control at all: stub ``_scaffolded_in`` to
    return the enrolled set and the file stayed green, which is the
    failure mode #324 is the record of. Every row here was a real miss.
    """

    @staticmethod
    def _found(source: str) -> set[tuple[str, str]]:
        return _scaffolded_in(astwalk.parse(source))

    def test_a_new_template_is_found(self) -> None:
        """The control the guard had none of."""
        body = '_create_if_missing(kstrl_dir / "x_prompt.md", DEFAULT_X_PROMPT, ui)\n'
        assert self._found(body) == {("x_prompt.md", "DEFAULT_X_PROMPT")}

    def test_an_assembled_filename_is_found(self) -> None:
        """``kstrl_dir / ("x" + "_prompt.md")`` walked past round 1,
        which read ``target.right`` only when it was a ``Constant``."""
        body = '_create_if_missing(kstrl_dir / ("x" + "_prompt.md"), DEFAULT_X_PROMPT, ui)\n'
        assert self._found(body) == {("x_prompt.md", "DEFAULT_X_PROMPT")}

    def test_a_writer_renamed_on_import_is_found(self) -> None:
        """The alias the disclosure used to call theoretical."""
        body = (
            "from kstrl.init_cmd import _create_if_missing as _cim\n"
            '_cim(kstrl_dir / "x_prompt.md", DEFAULT_X_PROMPT, ui)\n'
        )
        assert self._found(body) == {("x_prompt.md", "DEFAULT_X_PROMPT")}

    def test_a_body_reached_through_a_module_is_found(self) -> None:
        """``init_cmd.DEFAULT_X_PROMPT`` is an ``Attribute``, and round 1
        accepted a ``Name`` and nothing else."""
        body = '_create_if_missing(kstrl_dir / "x_prompt.md", init_cmd.DEFAULT_X_PROMPT, ui)\n'
        assert self._found(body) == {("x_prompt.md", "DEFAULT_X_PROMPT")}

    def test_a_write_that_is_not_a_prompt_body_is_not_a_hit(self) -> None:
        """The line the walk draws: a scaffolded file whose body is not a
        prompt constant is not a template, and ``ks init`` writes four."""
        body = '_create_if_missing(kstrl_dir / "progress.txt", DEFAULT_PROGRESS, ui)\n'
        assert self._found(body) == set()

    def test_prose_naming_the_writer_is_not_a_hit(self) -> None:
        """Why the walk reads calls rather than text: this very file has
        to spell the call it forbids in order to explain it."""
        body = '"""Adds _create_if_missing(kstrl_dir / \'x_prompt.md\', X, ui)."""\n'
        assert self._found(body) == set()

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_filename_the_interpreter_builds_is_a_known_miss(self) -> None:
        """The residual both layers share, stated rather than implied.

        A filename only the interpreter can produce folds to ``None``,
        so neither the pair walk nor the census sees it. The bound is
        that ``ks init`` writes a fixed set of files, so building one of
        their names at run time is a deliberate act.
        """
        astwalk.blind_spot(
            self._found,
            '_create_if_missing(kstrl_dir / "".join(parts), DEFAULT_X_PROMPT, ui)\n',
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_a_writer_rebound_to_a_local_is_a_known_miss(self) -> None:
        """Layer 2's own residual. ``_cim = _create_if_missing`` binds
        the function object to a name the resolver cannot follow, so the
        call reads as a call on something else. Layer 1 still counts the
        filename it writes, WHEN THE FILENAME FOLDS, which is why this
        one alone is a message gap rather than a hole. The row below is
        the case where it does not."""
        astwalk.blind_spot(
            self._found,
            '_cim = _create_if_missing\n_cim(kstrl_dir / "x_prompt.md", DEFAULT_X_PROMPT, ui)\n',
        )

    @pytest.mark.xfail(strict=True, raises=AssertionError)
    def test_the_two_residuals_together_are_a_hole_and_not_a_message_gap(self) -> None:
        """The row #324 round 2 found missing, and the general lesson.

        Both rows above are disclosed and pinned SEPARATELY, and each
        one's docstring is true on its own: a built filename leaves layer
        2 counting the writer, and a rebound writer leaves layer 1
        counting the filename. Their CONJUNCTION is neither, and it was
        pinned by neither. Measured: a fourth un-enrolled scaffolded
        template planted in ``kstrl/init_cmd.py`` with both residuals
        present gives 26 passed and 2 xfailed, #286 reproduced with the
        guard green.

        Pinning each residual does not pin their intersection, which is
        the reason this row exists and the reason a disclosure that
        appeals to ANOTHER layer has to name the case where that layer is
        also blind.
        """
        astwalk.blind_spot(
            self._found,
            '_cim = _create_if_missing\n_cim(kstrl_dir / "".join(parts), DEFAULT_X_PROMPT, ui)\n',
        )
