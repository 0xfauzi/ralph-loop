"""Evolve screen: proposals, failure patterns, experiment trends (D4).

Three tabs over the evolution layer's on-disk records:
- proposals: master-detail over .kstrl/proposals/prop-*.md with the
  REAL apply path (B1's engine). `a` opens the confirm modal; the
  modal IS the confirmation, so apply_proposal runs with an
  always-yes seam. Non-convention proposals keep the honest manual
  message - no false "applied" claims (R6.3).
- patterns: get_cross_run_patterns over the journal.
- trends: the last experiments.tsv rows with retry-rate bars and
  R3.1 lower-bound markers on token/cost cells.

Propose-from-TUI is deliberately absent in v1: `ks evolve` remains
the generator; this screen reads, triages, and applies.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import DataTable, Footer, Static, TabbedContent, TabPane

from kstrl.evolution import EvolutionConfig, EvolutionJournal
from kstrl.interaction import PromptKind, PromptRequest
from kstrl.proposals import Proposal, apply_proposal, list_proposals
from kstrl.tui import theme
from kstrl.tui.screens.options import OptionsModal
from kstrl.tui.widgets.config_problem import ConfigProblemBanner
from kstrl.tui.widgets.context_bar import ContextBar

TREND_ROWS = 14
_BAR_BLOCKS = "▁▂▃▄▅▆▇"


def retry_bar(rate: float) -> str:
    """A one-cell bar for a 0..1 retry rate; empty stays empty."""
    if not math.isfinite(rate) or rate <= 0:
        return theme.EMPTY_CELL
    index = min(len(_BAR_BLOCKS) - 1, int(rate * len(_BAR_BLOCKS)))
    return _BAR_BLOCKS[index]


def _proposal_detail(proposal: Proposal, root_dir: Path) -> Text:
    text = Text()
    text.append(f"{proposal.display_id} ", style=f"bold {theme.ACCENT}")
    text.append(proposal.title, style="bold")
    try:
        shown_path = proposal.path.relative_to(root_dir)
    except ValueError:
        shown_path = proposal.path
    text.append(f"\n{shown_path}", style=theme.MUTED)
    text.append("\ntype ", style=theme.MUTED)
    text.append(proposal.type or "?")
    text.append("  target ", style=theme.MUTED)
    text.append(proposal.target or "?")
    if proposal.convention:
        text.append("\n\nsuggested change\n", style=f"bold {theme.ACCENT}")
        text.append(proposal.convention)
    if proposal.applied:
        text.append(f"\n\n✓ applied {proposal.applied}", style=f"bold {theme.SUCCESS}")
    elif proposal.is_convention:
        text.append("\n\n(a) apply - appends to CLAUDE.md Agent Learnings", style=theme.MUTED)
    else:
        text.append(
            "\n\nautomated apply only covers convention-type proposals "
            "(target claude_md); review the file and apply manually",
            style=theme.WARNING,
        )
    return text


class EvolveScreen(Screen[None]):
    BINDINGS = [
        Binding("escape", "app.pop_screen", "Back"),
        Binding("a", "apply_selected", "Apply"),
        Binding("r", "reload", "Reload", show=False),
    ]

    PROPOSAL_COLUMNS = ("id", "title", "type", "target", "applied")
    PATTERN_COLUMNS = ("check", "code", "runs", "components", "category")
    TREND_COLUMNS = ("run", "done", "failed", "retry", "tok", "cost")

    def __init__(self) -> None:
        super().__init__()
        self._proposals: list[Proposal] = []

    def compose(self) -> ComposeResult:
        yield ContextBar("evolve", "the harness improving itself")
        # Above the tabs, not inside one: an unreadable [evolution]
        # section empties patterns AND trends, and the operator may be
        # looking at proposals when it happens.
        yield ConfigProblemBanner()
        # Also above the tabs, and for the same reason: a repaired
        # journal write is about the file both journal-backed tabs read,
        # and the operator may be looking at proposals when it happens.
        # A separate widget rather than a second use of the banner
        # above, which prefixes "configuration unreadable": the config
        # is fine here and the journal was torn (#333).
        yield Static(id="evolve-repairs")
        with TabbedContent(id="evolve-tabs"):
            with TabPane("proposals", id="tab-proposals"):
                with Horizontal(id="proposals-split"):
                    yield DataTable(id="proposals-table")
                    yield Static(id="proposal-detail")
            with TabPane("patterns", id="tab-patterns"):
                yield DataTable(id="patterns-table")
            with TabPane("trends", id="tab-trends"):
                yield DataTable(id="trends-table")
        yield Footer()

    @property
    def ready(self) -> bool:
        return next(iter(self.query(TabbedContent)), None) is not None

    def on_mount(self) -> None:
        for table_id, columns in (
            ("#proposals-table", self.PROPOSAL_COLUMNS),
            ("#patterns-table", self.PATTERN_COLUMNS),
            ("#trends-table", self.TREND_COLUMNS),
        ):
            table = self.query_one(table_id, DataTable)
            table.cursor_type = "row"
            table.zebra_stripes = False
            for column in columns:
                table.add_column(column, key=column)
        self.reload()

    def _root_dir(self) -> Path:
        root = getattr(self.app, "root_dir", None)
        return root if root is not None else Path.cwd()

    def reload(self) -> None:
        root_dir = self._root_dir()
        self._load_proposals(root_dir)
        self._load_patterns_and_trends(root_dir)
        pending = sum(1 for p in self._proposals if not p.applied)
        right = Text()
        if pending:
            right.append(f"▲ {pending} pending", style=theme.WARNING)
            right.append(
                f" of {len(self._proposals)} proposal(s)",
                style=theme.MUTED,
            )
        else:
            right.append(
                f"{len(self._proposals)} proposal(s)",
                style=theme.MUTED,
            )
        self.query_one(ContextBar).set_right(right)

    def _load_proposals(self, root_dir: Path) -> None:
        self._proposals = list_proposals(root_dir / ".kstrl" / "proposals")
        table = self.query_one("#proposals-table", DataTable)
        table.clear()
        for proposal in self._proposals:
            applied = (
                Text("✓", style=f"bold {theme.SUCCESS}")
                if proposal.applied
                else Text(theme.EMPTY_CELL, style=theme.MUTED)
            )
            table.add_row(
                Text(proposal.display_id, style="bold"),
                Text(proposal.title),
                Text(proposal.type) if proposal.type else Text(theme.EMPTY_CELL, style=theme.MUTED),
                Text(proposal.target)
                if proposal.target
                else Text(theme.EMPTY_CELL, style=theme.MUTED),
                applied,
                key=proposal.path.name,
            )
        detail = self.query_one("#proposal-detail", Static)
        if self._proposals:
            self._show_detail(0)
        else:
            detail.update(
                Text(
                    "no proposals yet - run `ks evolve` after a few factory runs to generate them",
                    style=theme.MUTED,
                )
            )

    def _load_patterns_and_trends(self, root_dir: Path) -> None:
        """Both journal-backed tabs, or the reason neither can be shown.

        Guarded because this screen is reachable from the home shell,
        which is not a click command and so never runs the entry check
        that would have stopped `ks evolve` (#289). Degrading to two
        empty tables would be worse than the traceback it replaces:
        "no patterns yet" is a real state on this screen.
        """
        config = self.query_one(ConfigProblemBanner).load(EvolutionConfig.load, root_dir)
        patterns_table = self.query_one("#patterns-table", DataTable)
        patterns_table.clear()
        trends_table = self.query_one("#trends-table", DataTable)
        trends_table.clear()
        if config is None:
            self._show_repairs(None)
            return
        journal = EvolutionJournal(config)
        self._show_repairs(journal)
        for pattern in journal.get_cross_run_patterns():
            patterns_table.add_row(
                Text(pattern.check_name, style="bold"),
                Text(pattern.error_signature),
                Text(str(pattern.frequency), justify="right"),
                Text(str(len(pattern.affected_components)), justify="right"),
                Text(pattern.category, style=theme.MUTED),
            )
        for row in journal.get_experiment_trends(last_n=TREND_ROWS):
            trends_table.add_row(*self._trend_cells(row))

    def _show_repairs(self, journal: EvolutionJournal | None) -> None:
        """The count of repaired journal writes, or nothing at zero (#333).

        #312's argument for writing a durable ``journal_repair`` row at
        all was that under the TUI the logger warning goes to
        ``orchestrator.log`` where nobody is looking. That argument names
        the TUI operator, and until now this screen was the one surface
        that built an ``EvolutionJournal``, read the journal for its
        patterns tab, and said nothing about the rows.

        Silent at zero, which is the same choice ``ks evolve --status``
        makes and for the same reason: a line that prints on every
        healthy journal is a line an operator learns to skip. The facts
        are the CLI's facts, because they are facts about the file
        rather than about a surface. What is NOT shared is the helper:
        ``cli._echo_journal_repairs`` writes through ``UI`` in the click
        module, so this is a second renderer of one measurement, not a
        second measurement.

        Takes the journal rather than a count so the count and the path
        printed beside it cannot come from two different journals. None
        means the config did not resolve, which the banner above has
        already said; this line hides rather than showing a stale count
        from before a ``reload``.

        COST, and it is a whole extra read of the journal rather than
        nothing: ``get_repair_count`` goes through
        ``_read_all_entries`` while the patterns tab beside it goes
        through ``_read_journal_entries``, so this screen reads the file
        twice per load and per ``r``. Measured here, 20 calls each on a
        warm cache: 0.016 ms at 1 line, 0.097 ms at 100, 1.12 ms at 989
        (194 KiB, which is the largest real journal #333 found), 12.7 ms
        at 10,000 lines. Linear in the file, and the last of those is
        the one to watch if journals ever get that big.

        The window is NOT the reason it is a second read, and that
        sentence used to be here and was wrong (#352 round 2, F5).
        ``get_cross_run_patterns`` goes through ``_read_journal_entries``,
        which calls ``_read_all_entries()`` and applies its lookback in
        MEMORY, so both reads take the whole file and the window costs
        nothing at the read. The file is therefore read twice per load
        and per ``r``, on the Textual event loop. Measured directly, 20
        calls each on a 10,000-line journal: ``get_repair_count`` 9.3 ms
        and ``get_cross_run_patterns`` 9.2 ms, so the screen pays
        18.5 ms where one read would cost 9.3. The two are within a few
        percent of each other, which is the window costing nothing.

        Kept as a second read anyway, and this is the real reason: the
        two are methods on the same journal called from two places on
        this screen, so folding them means either passing the entries
        into ``_show_repairs`` or giving ``EvolutionJournal`` a cached
        read. The first makes this method's own argument a list somebody
        else read, which is the thing the paragraph above rejects for
        the count and the path. The second changes the surface every
        other caller of the journal sees, for a screen that reloads on
        one keypress. Neither is worth 12.7 ms at a file size no journal
        here has reached; the cost is written down so the next reader
        decides on the number rather than on the sentence.
        """
        line = self.query_one("#evolve-repairs", Static)
        if journal is None or not (repairs := journal.get_repair_count()):
            line.display = False
            return
        line.display = True
        line.update(
            Text(
                f"▲ journal: {repairs} interrupted write(s) repaired. A crash left "
                f"{journal.config.journal_path} without a trailing newline. The line "
                "above each journal_repair row is what that write left behind: either "
                "a torn fragment, which is lost, or a whole record that lost only its "
                "newline, which is readable again. Read it to tell which.",
                style=theme.WARNING,
            )
        )

    @staticmethod
    def _trend_cells(row: dict[str, Any]) -> tuple[Text | str, ...]:
        def _num(key: str) -> Text:
            value = str(row.get(key, "") or "")
            if not value:
                return Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right")
            return Text(value, justify="right")

        run_id = str(row.get("run_id", ""))
        short = run_id.rsplit("-", 1)[-1] if run_id else theme.EMPTY_CELL
        try:
            rate = float(row.get("retry_rate", "") or 0)
        except ValueError:
            rate = 0.0
        if not math.isfinite(rate):
            rate = 0.0
        # Unreported calls make token/cost totals lower bounds (R3.1).
        try:
            unreported = int(float(row.get("unreported_calls", "") or 0))
        except (OverflowError, ValueError):
            unreported = 0
        marker = "+" if unreported else ""
        tokens = str(row.get("total_tokens", "") or "")
        cost = str(row.get("total_cost_usd", "") or "")
        return (
            Text(short, style="bold"),
            _num("completed"),
            _num("failed"),
            Text(f"{retry_bar(rate)} {rate:.2f}" if rate else theme.EMPTY_CELL, justify="right"),
            Text(f"{tokens}{marker}", justify="right")
            if tokens
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
            Text(f"${cost}{marker}", justify="right")
            if cost
            else Text(theme.EMPTY_CELL, style=theme.MUTED, justify="right"),
        )

    # -- proposals master-detail + apply ------------------------------------

    def _selected_proposal(self) -> Proposal | None:
        table = self.query_one("#proposals-table", DataTable)
        if not table.row_count or table.cursor_row is None:
            return None
        index = table.cursor_row
        if 0 <= index < len(self._proposals):
            return self._proposals[index]
        return None

    def _show_detail(self, index: int) -> None:
        if 0 <= index < len(self._proposals):
            self.query_one("#proposal-detail", Static).update(
                _proposal_detail(self._proposals[index], self._root_dir()),
            )

    def on_data_table_row_highlighted(
        self,
        event: DataTable.RowHighlighted,
    ) -> None:
        if event.data_table.id != "proposals-table":
            return
        if event.cursor_row is not None and event.cursor_row >= 0:
            self._show_detail(event.cursor_row)

    def action_reload(self) -> None:
        self.reload()

    def action_apply_selected(self) -> None:
        if self.query_one(TabbedContent).active != "tab-proposals":
            return
        proposal = self._selected_proposal()
        if proposal is None:
            return
        if proposal.applied:
            self.app.notify(
                f"{proposal.display_id} already applied at {proposal.applied}",
            )
            return
        if not proposal.is_convention:
            self.app.notify(
                "automated apply only covers convention-type proposals "
                f"(target claude_md); review {proposal.path} and apply "
                "manually",
                severity="warning",
            )
            return

        def _resolved(choice: int | None) -> None:
            if choice != 0:
                return
            # The modal WAS the confirmation.
            outcome = apply_proposal(
                proposal,
                self._root_dir(),
                confirm=lambda _: True,
            )
            self.app.notify(
                outcome.message,
                severity="information" if outcome.status == "applied" else "error",
            )
            self._load_proposals(self._root_dir())

        self.app.push_screen(
            OptionsModal(
                PromptRequest(
                    kind=PromptKind.CONFIRM,
                    header=(
                        f"{proposal.display_id}: append this convention to "
                        f'CLAUDE.md Agent Learnings?  "{proposal.convention}"'
                    ),
                    options=("Apply", "Cancel"),
                    default=1,
                )
            ),
            _resolved,
        )
