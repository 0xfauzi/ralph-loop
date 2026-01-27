"""Textual-based TUI implementation."""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Literal

from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.events import Key
from textual.message import Message
from textual.screen import ModalScreen
from textual.theme import Theme
from textual.widgets import (
    Button,
    ContentSwitcher,
    DataTable,
    Footer,
    Label,
    ListItem,
    ListView,
    LoadingIndicator,
    Markdown,
    ProgressBar,
    Rule,
    Static,
    TabbedContent,
    TabPane,
)
from rich.text import Text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import TextIO

    from ralph_py.ui.base import UI

ITERATION_RE = re.compile(r"Iteration\\s+(\\d+)\\s*/\\s*(\\d+)")
DEFAULT_DETAIL_TEXT = "_No data yet._"

TAG_CLASSES = {
    "AI": "tag-ai",
    "USER": "tag-user",
    "PROMPT": "tag-prompt",
    "THINK": "tag-think",
    "SYS": "tag-sys",
    "TOOL": "tag-tool",
    "GIT": "tag-git",
    "GUARD": "tag-guard",
    "PLAN": "tag-plan",
    "REPORT": "tag-report",
    "ACTIONS": "tag-actions",
}

STREAM_BADGE_CLASSES = {
    "AI": "badge-ai",
    "SYS": "badge-sys",
    "TOOL": "badge-tool",
    "USER": "badge-user",
}

TAG_WIDTH = max(len(tag) for tag in TAG_CLASSES)
SUMMARY_TAGS = {"PLAN", "ACTIONS", "REPORT", "FINAL"}
DIFF_PREFIXES = ("diff --git", "index ")
DIFF_FILES = ("+++ ", "--- ")
ButtonVariant = Literal["default", "primary", "success", "warning", "error"]


def _diff_line_class(line: str) -> str:
    if line.startswith(DIFF_PREFIXES):
        return "diff-meta"
    if line.startswith("@@"):
        return "diff-hunk"
    if line.startswith(DIFF_FILES):
        return "diff-file"
    if line.startswith("+") and not line.startswith("+++ ") and not line.startswith("+ "):
        return "diff-add"
    if line.startswith("-") and not line.startswith("--- ") and not line.startswith("- "):
        return "diff-del"
    return ""


@dataclass
class PromptRequest:
    kind: str
    header: str
    options: list[str]
    default: int | None
    event: threading.Event
    result: dict[str, Any]


@dataclass
class SessionState:
    config: dict[str, str] = field(default_factory=dict)
    sys_content: str = ""


@dataclass
class IterationState:
    index: int
    max_iterations: int | None = None
    steps_total: int = 3
    steps_done: set[str] = field(default_factory=set)
    plan: str = ""
    actions: str = ""
    report: str = ""
    final: str = ""
    streaming: bool = False
    card: "SummaryCard | None" = None
    detail: "IterationDetail | None" = None


class HeaderUpdateMessage(Message):
    def __init__(
        self,
        title: str | None = None,
        iteration_label: str | None = None,
        iteration_index: int | None = None,
        max_iterations: int | None = None,
        agent: str | None = None,
        status: str | None = None,
        streaming: bool | None = None,
    ) -> None:
        super().__init__()
        self.title = title
        self.iteration_label = iteration_label
        self.iteration_index = iteration_index
        self.max_iterations = max_iterations
        self.agent = agent
        self.status = status
        self.streaming = streaming


class IterationStartMessage(Message):
    def __init__(self, index: int, max_iterations: int | None) -> None:
        super().__init__()
        self.index = index
        self.max_iterations = max_iterations


class TimelineMessage(Message):
    def __init__(
        self,
        kind: str,
        text: str | None = None,
        key: str | None = None,
        value: str | None = None,
        tag: str | None = None,
        title: str | None = None,
        content: str | None = None,
        level: str | None = None,
    ) -> None:
        super().__init__()
        self.kind = kind
        self.text = text
        self.key = key
        self.value = value
        self.tag = tag
        self.title = title
        self.content = content
        self.level = level


class StreamBlockStartMessage(Message):
    def __init__(self, channel: str, title: str) -> None:
        super().__init__()
        self.channel = channel
        self.title = title


class StreamBlockEndMessage(Message):
    def __init__(self, channel: str, title: str) -> None:
        super().__init__()
        self.channel = channel
        self.title = title


class StreamLineMessage(Message):
    def __init__(self, tag: str, line: str) -> None:
        super().__init__()
        self.tag = tag
        self.line = line


class SummaryUpdateMessage(Message):
    def __init__(
        self,
        iteration: int | None,
        tag: str,
        title: str,
        content: str,
    ) -> None:
        super().__init__()
        self.iteration = iteration
        self.tag = tag
        self.title = title
        self.content = content


class ConfigUpdateMessage(Message):
    def __init__(self, key: str, value: str) -> None:
        super().__init__()
        self.key = key
        self.value = value


class PromptRequestMessage(Message):
    def __init__(self, request: PromptRequest) -> None:
        super().__init__()
        self.request = request


class EmptyState(Vertical):
    def __init__(self, title: str, body: str) -> None:
        super().__init__(classes="empty-state")
        self._title = title
        self._body = body

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="empty-title")
        yield LoadingIndicator(classes="empty-spinner")
        yield Static(self._body, classes="empty-body", markup=False)


class SummaryPlaceholderCard(ListItem):
    def __init__(self, text: str) -> None:
        super().__init__()
        self._text = text

    def compose(self) -> ComposeResult:
        with Horizontal(classes="summary-empty"):
            yield LoadingIndicator(classes="summary-placeholder-spinner")
            yield Label(self._text, classes="summary-placeholder", markup=False)


class SummaryCard(ListItem):
    def __init__(
        self,
        key: str,
        title: str,
        subtitle: str,
        total_steps: int = 1,
    ) -> None:
        super().__init__()
        self.key = key
        self.detail_id = key
        self._title = title
        self._subtitle = subtitle
        self._total_steps = total_steps
        self._progress: ProgressBar | None = None
        self._spinner: LoadingIndicator | None = None
        self._status: Label | None = None
        self._pending_progress: tuple[int, int] | None = None
        self._pending_status: str | None = None
        self._pending_spinner: bool | None = None

    def compose(self) -> ComposeResult:
        with Vertical(classes="summary-card-body"):
            with Horizontal():
                yield Label(self._title, classes="summary-title", expand=True)
                yield LoadingIndicator(
                    classes="summary-spinner hidden",
                    id="summary-spinner",
                )
            yield Label(self._subtitle, classes="summary-status", id="summary-status")
            yield ProgressBar(
                total=self._total_steps,
                show_percentage=False,
                show_eta=False,
                id="summary-progress",
            )

    def on_mount(self) -> None:
        self._progress = self.query_one("#summary-progress", ProgressBar)
        self._spinner = self.query_one("#summary-spinner", LoadingIndicator)
        self._status = self.query_one("#summary-status", Label)
        if self._pending_progress:
            done, total = self._pending_progress
            self.set_progress(done, total)
        if self._pending_status is not None:
            self.set_status(self._pending_status)
        if self._pending_spinner is not None:
            self.set_spinner(self._pending_spinner)

    def set_progress(self, done: int, total: int) -> None:
        if self._progress is None:
            self._pending_progress = (done, total)
            return
        self._progress.update(total=total, progress=done)

    def set_status(self, text: str) -> None:
        if self._status is None:
            self._pending_status = text
            return
        self._status.update(text)

    def set_spinner(self, active: bool) -> None:
        if self._spinner is None:
            self._pending_spinner = active
            return
        if active:
            self._spinner.remove_class("hidden")
        else:
            self._spinner.add_class("hidden")


class SectionHeader(Vertical):
    def __init__(self, text: str) -> None:
        super().__init__(classes="section-header")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Label(self._text, classes="section-title")
        yield Rule(line_style="dashed", classes="section-rule")


class SubsectionHeader(Vertical):
    def __init__(self, text: str) -> None:
        super().__init__(classes="subsection-header")
        self._text = text

    def compose(self) -> ComposeResult:
        yield Label(self._text, classes="subsection-title")


class KeyValueTable(Vertical):
    def __init__(self, title: str) -> None:
        super().__init__(classes="kv-table")
        self._title = title
        self._table: DataTable[Any] = DataTable(zebra_stripes=True, show_header=True)
        self._table.add_columns(("Key", "key"), ("Value", "value"))
        self._table.show_cursor = False
        self._table.cursor_type = "none"
        self._rows: set[str] = set()

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="kv-table-title")
        yield self._table

    def add_row(self, key: str, value: str) -> None:
        if not key:
            return
        key_cell = Text(key, style="bold #8aa0b6")
        value_cell = Text(value, style="#e3e9f0") if value else Text("")
        if key in self._rows:
            self._table.update_cell(key, "value", value_cell)
            return
        self._table.add_row(key_cell, value_cell, key=key)
        self._rows.add(key)


class SessionDetail(Vertical):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._config: dict[str, str] = {}
        self._sys_content = ""
        self._config_table: DataTable[Any] = DataTable(zebra_stripes=True, show_header=True)
        self._config_table.add_columns(("Key", "key"), ("Value", "value"))
        self._config_table.show_cursor = False
        self._config_table.cursor_type = "none"
        self._sys_markdown = Markdown("", classes="detail-markdown")
        self._refresh_config()
        self._refresh_sys()

    def compose(self) -> ComposeResult:
        yield Label("Config", classes="detail-section-title")
        yield self._config_table
        yield Label("System", classes="detail-section-title")
        yield self._sys_markdown

    def update_config(self, config: dict[str, str]) -> None:
        self._config = dict(config)
        self._refresh_config()

    def update_sys(self, content: str) -> None:
        self._sys_content = content
        self._refresh_sys()

    def _refresh_config(self) -> None:
        self._config_table.clear()
        if not self._config:
            self._config_table.add_row(
                Text("No data yet", style="italic #8aa0b6"),
                Text(""),
                key="empty",
            )
            return
        for key, value in self._config.items():
            self._config_table.add_row(
                Text(key, style="bold #8aa0b6"),
                Text(value, style="#e3e9f0") if value else Text(""),
                key=key,
            )

    def _refresh_sys(self) -> None:
        self._sys_markdown.update(self._sys_content or DEFAULT_DETAIL_TEXT)


class IterationDetail(Vertical):
    def __init__(self, iteration: int, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.iteration = iteration
        self._plan = ""
        self._actions = ""
        self._report = ""
        self._final = ""
        self._plan_md: Markdown | None = None
        self._actions_md: Markdown | None = None
        self._report_md: Markdown | None = None
        self._final_md: Markdown | None = None

    def compose(self) -> ComposeResult:
        yield Label(f"Iteration {self.iteration}", classes="detail-title")
        with TabbedContent(classes="detail-tabs"):
            with TabPane("Plan", classes="detail-tab-pane"):
                yield Markdown("", classes="detail-plan")
            with TabPane("Actions", classes="detail-tab-pane"):
                yield Markdown("", classes="detail-actions")
            with TabPane("Report", classes="detail-tab-pane"):
                yield Markdown("", classes="detail-report")
            with TabPane("Final", classes="detail-tab-pane"):
                yield Markdown("", classes="detail-final")

    def on_mount(self) -> None:
        self._plan_md = self.query_one(".detail-plan", Markdown)
        self._actions_md = self.query_one(".detail-actions", Markdown)
        self._report_md = self.query_one(".detail-report", Markdown)
        self._final_md = self.query_one(".detail-final", Markdown)
        self.update_plan(self._plan)
        self.update_actions(self._actions)
        self.update_report(self._report)
        self.update_final(self._final)

    def update_plan(self, content: str) -> None:
        self._plan = content
        if self._plan_md:
            self._plan_md.update(content or DEFAULT_DETAIL_TEXT)

    def update_actions(self, content: str) -> None:
        self._actions = content
        if self._actions_md:
            self._actions_md.update(content or DEFAULT_DETAIL_TEXT)

    def update_report(self, content: str) -> None:
        self._report = content
        if self._report_md:
            self._report_md.update(content or DEFAULT_DETAIL_TEXT)

    def update_final(self, content: str) -> None:
        self._final = content
        if self._final_md:
            self._final_md.update(content or DEFAULT_DETAIL_TEXT)


class PanelCard(Vertical):
    def __init__(self, title: str, content: str) -> None:
        super().__init__(classes="panel-card")
        self._title = title
        self._content = content

    def compose(self) -> ComposeResult:
        yield Label(self._title, classes="panel-title")
        yield Markdown(self._content or DEFAULT_DETAIL_TEXT, classes="panel-body")


class StreamRow(Horizontal):
    def __init__(self, tag: str, line: str, tag_width: int, show_tag: bool) -> None:
        tag_name = tag.lower()
        row_class = f"stream-row line-{tag_name}" if tag else "stream-row"
        diff_class = _diff_line_class(line)
        if diff_class:
            row_class = f"{row_class} {diff_class}"
        super().__init__(classes=row_class)
        self._tag = tag
        self._line = line
        self._tag_width = tag_width
        self._show_tag = show_tag

    def compose(self) -> ComposeResult:
        tag_class = TAG_CLASSES.get(self._tag.upper(), "")
        tag_label = Label(
            self._tag if self._show_tag else "",
            classes=f"stream-tag {tag_class}".strip(),
            markup=False,
        )
        tag_label.styles.width = self._tag_width + 2
        tag_label.styles.content_align_horizontal = "right"
        yield tag_label
        yield Static(self._line, classes="stream-content", markup=False, expand=True)


class StreamBlock(Vertical):
    def __init__(self, channel: str, title: str) -> None:
        channel_class = f"stream-{channel.lower()}"
        super().__init__(classes=f"stream-block {channel_class}")
        self._channel = channel.upper()
        self._title = title
        self._badge_class = STREAM_BADGE_CLASSES.get(self._channel, "badge-default")
        self._body: Vertical | None = None
        self._placeholder: Static | None = None
        self._last_tag: str | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(classes="stream-block-header"):
            yield Label(
                self._channel,
                classes=f"stream-badge {self._badge_class}",
                markup=False,
            )
            yield Label(self._title, classes="stream-block-title", markup=False)
        self._placeholder = Static(
            "Awaiting output...",
            classes="stream-body-placeholder",
            markup=False,
        )
        self._body = Vertical(self._placeholder, classes="stream-body")
        yield self._body

    def add_line(self, tag: str, line: str, tag_width: int) -> None:
        if self._body is None:
            return
        if self._placeholder is not None:
            self._placeholder.remove()
            self._placeholder = None
        show_tag = tag != self._last_tag
        row = StreamRow(tag, line, tag_width, show_tag=show_tag)
        self._last_tag = tag
        self._body.mount(row)


class ChooseModal(ModalScreen[int]):
    def __init__(self, header: str, options: list[str], default: int) -> None:
        super().__init__()
        self._header = header
        self._options = options
        self._default = default

    def compose(self) -> ComposeResult:
        with Container(classes="prompt-dialog"):
            yield Label(self._header, classes="prompt-header")
            with Vertical(classes="prompt-buttons"):
                for idx, option in enumerate(self._options):
                    variant: ButtonVariant = "primary" if idx == self._default else "default"
                    yield Button(
                        f"{idx + 1}. {option}",
                        id=f"opt-{idx}",
                        variant=variant,
                    )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id.startswith("opt-"):
            idx = int(button_id.split("-")[-1])
            self.dismiss(idx)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    def __init__(self, prompt: str, default: bool) -> None:
        super().__init__()
        self._prompt = prompt
        self._default = default

    def compose(self) -> ComposeResult:
        with Container(classes="prompt-dialog"):
            yield Label(self._prompt, classes="prompt-header")
            with Vertical(classes="prompt-buttons"):
                yes_variant: ButtonVariant = "primary" if self._default else "default"
                no_variant: ButtonVariant = "primary" if not self._default else "default"
                yield Button("Yes", id="confirm-yes", variant=yes_variant)
                yield Button("No", id="confirm-no", variant=no_variant)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        button_id = event.button.id or ""
        if button_id == "confirm-yes":
            self.dismiss(True)
        elif button_id == "confirm-no":
            self.dismiss(False)

    def on_key(self, event: Key) -> None:
        if event.key == "escape":
            self.dismiss(None)


class TextualApp(App[Any]):
    CSS_PATH = "textual.tcss"

    BINDINGS = [
        ("q", "quit", "Quit"),
        ("s", "toggle_scroll", "Toggle scroll"),
    ]

    def __init__(self, runner: Callable[[UI], Any], mode_label: str) -> None:
        super().__init__()
        self.register_theme(
            Theme(
                name="ralph-slate",
                primary="#6aa2ff",
                secondary="#8aa0b6",
                accent="#f1c27a",
                warning="#f1c27a",
                error="#e07a7a",
                success="#7cc7a5",
                foreground="#e3e9f0",
                background="#0b1016",
                surface="#141a22",
                panel="#1a2330",
                variables={
                    "block-cursor-background": "#6aa2ff",
                    "block-cursor-foreground": "#0b1016",
                    "block-cursor-text-style": "bold",
                    "block-cursor-blurred-background": "#1d2632",
                    "block-cursor-blurred-foreground": "#c7d2de",
                    "footer-key-foreground": "#6aa2ff",
                    "button-color-foreground": "#0b1016",
                    "button-focus-text-style": "bold",
                    "markdown-h1-color": "#f1c27a",
                    "markdown-h1-background": "#1a2330",
                    "markdown-h1-text-style": "bold",
                    "markdown-h2-color": "#f1c27a",
                    "markdown-h2-background": "transparent",
                    "markdown-h2-text-style": "bold",
                    "markdown-h3-color": "#8aa0b6",
                    "markdown-h3-background": "transparent",
                    "markdown-h3-text-style": "bold",
                },
            )
        )
        self.theme = "ralph-slate"
        self._runner = runner
        self._mode_label = mode_label
        self._ui: TextualUI | None = None
        self.exit_code: int = 0
        self._finished = False
        self._title = "Ralph"
        self._agent = ""
        self._status = ""
        self._iteration_label = ""
        self._current_iteration: int | None = None
        self._max_iterations: int | None = None
        self._streaming = False
        self._auto_scroll = True
        self._iterations: dict[int, IterationState] = {}
        self._session_state = SessionState()
        self._current_stream_block: StreamBlock | None = None
        self._timeline: VerticalScroll | None = None
        self._summary_rail: ListView | None = None
        self._summary_detail: ContentSwitcher | None = None
        self._session_detail: SessionDetail | None = None
        self._summary_cards: dict[str, SummaryCard] = {}
        self._summary_placeholder: SummaryPlaceholderCard | None = None
        self._stream_placeholder: EmptyState | None = None
        self._kv_block: KeyValueTable | None = None
        self._header_title: Label | None = None
        self._header_mode: Label | None = None
        self._header_agent: Label | None = None
        self._header_iteration: Label | None = None
        self._header_progress: ProgressBar | None = None
        self._header_spinner: LoadingIndicator | None = None
        self._header_status: Label | None = None

    def compose(self) -> ComposeResult:
        with Horizontal(id="header"):
            yield Label(self._title, id="header-title")
            yield Label("", id="header-mode", classes="header-badge")
            yield Label("", id="header-agent", classes="header-badge hidden")
            yield Label("", id="header-iteration", classes="header-badge hidden")
            yield ProgressBar(
                total=None,
                show_percentage=False,
                show_eta=False,
                id="header-progress",
                classes="hidden",
            )
            yield LoadingIndicator(id="header-spinner", classes="hidden")
            yield Label("", id="header-status")
        with Horizontal(id="main-body"):
            yield VerticalScroll(id="timeline")
            with Vertical(id="inspector"):
                yield Label("Summaries", classes="inspector-title")
                yield Rule(line_style="dashed", classes="inspector-rule")
                yield ListView(id="summary-rail")
                yield Label("Details", classes="inspector-title")
                yield Rule(line_style="dashed", classes="inspector-rule")
                yield ContentSwitcher(id="summary-detail")
        yield Footer(id="footer")

    def on_mount(self) -> None:
        self._ui = TextualUI(self)
        self._timeline = self.query_one("#timeline", VerticalScroll)
        self._summary_rail = self.query_one("#summary-rail", ListView)
        self._summary_detail = self.query_one("#summary-detail", ContentSwitcher)
        self._header_title = self.query_one("#header-title", Label)
        self._header_mode = self.query_one("#header-mode", Label)
        self._header_agent = self.query_one("#header-agent", Label)
        self._header_iteration = self.query_one("#header-iteration", Label)
        self._header_progress = self.query_one("#header-progress", ProgressBar)
        self._header_spinner = self.query_one("#header-spinner", LoadingIndicator)
        self._header_status = self.query_one("#header-status", Label)

        if self._timeline:
            stream_placeholder = EmptyState(
                "Waiting for agent output",
                "Stream output will appear here once the agent starts.",
            )
            self._timeline.mount(stream_placeholder)
            self._stream_placeholder = stream_placeholder

        session_detail = SessionDetail(id="session")
        if self._summary_detail:
            self._summary_detail.mount(session_detail)
            self._summary_detail.current = "session"
        self._session_detail = session_detail

        session_card = SummaryCard("session", "Session", "Config and system info", 1)
        session_card.add_class("summary-session", "summary-complete")
        session_card.set_status("Ready")
        self._summary_cards["session"] = session_card
        if self._summary_rail:
            self._summary_rail.append(session_card)
            summary_placeholder = SummaryPlaceholderCard("Waiting for iteration summaries")
            self._summary_placeholder = summary_placeholder
            self._summary_rail.append(summary_placeholder)
            self._summary_rail.index = 0
        session_card.set_progress(1, 1)

        self._update_header()
        self.run_worker(self._run_runner, thread=True)

    def _clear_stream_placeholder(self) -> None:
        if self._stream_placeholder is None:
            return
        self._stream_placeholder.remove()
        self._stream_placeholder = None

    def _run_runner(self) -> None:
        if self._ui is None:
            self.exit_code = 1
            self.call_from_thread(self.exit, 1)
            return
        try:
            result = self._runner(self._ui)
            if hasattr(result, "exit_code"):
                self.exit_code = int(result.exit_code)
            elif isinstance(result, int):
                self.exit_code = result
            else:
                self.exit_code = 0
        except Exception as exc:  # pragma: no cover - safety net
            self.exit_code = 1
            self._ui.err(f"Unhandled error: {exc}")
        self.call_from_thread(self._mark_finished)

    def _mark_finished(self) -> None:
        self._finished = True
        if self.exit_code == 0:
            self._status = "Complete - press q to exit"
        else:
            self._status = "Failed - press q to exit"
        self._update_header()
        if self._ui is not None:
            self._ui.info("Run complete. Press q to exit.")

    async def action_quit(self) -> None:
        self.exit(self.exit_code)

    def action_toggle_scroll(self) -> None:
        self._auto_scroll = not self._auto_scroll
        status = "scroll: follow" if self._auto_scroll else "scroll: locked"
        self._status = status
        self._update_header()

    def _update_header(self) -> None:
        if self._header_title:
            self._header_title.update(self._title)
        if self._header_mode:
            self._header_mode.update(f"mode {self._mode_label}")
        if self._header_agent:
            if self._agent:
                self._header_agent.update(f"agent {self._agent}")
                self._header_agent.remove_class("hidden")
            else:
                self._header_agent.add_class("hidden")
        if self._header_iteration:
            if self._iteration_label:
                self._header_iteration.update(f"iter {self._iteration_label}")
                self._header_iteration.remove_class("hidden")
            else:
                self._header_iteration.add_class("hidden")
        if self._header_status:
            self._header_status.update(self._status)
            self._header_status.remove_class(
                "status-ok",
                "status-warn",
                "status-err",
            )
            if self._status:
                self._header_status.remove_class("hidden")
            else:
                self._header_status.add_class("hidden")
            if self._status.startswith("OK"):
                self._header_status.add_class("status-ok")
            elif self._status.startswith("WARN"):
                self._header_status.add_class("status-warn")
            elif self._status.startswith("ERROR"):
                self._header_status.add_class("status-err")
        if self._header_spinner:
            if self._streaming:
                self._header_spinner.remove_class("hidden")
            else:
                self._header_spinner.add_class("hidden")
        if self._header_progress:
            if self._max_iterations and self._current_iteration:
                self._header_progress.update(
                    total=self._max_iterations,
                    progress=self._current_iteration,
                )
                self._header_progress.remove_class("hidden")
            else:
                self._header_progress.add_class("hidden")

    def _scroll_timeline_end(self) -> None:
        if not self._auto_scroll or not self._timeline:
            return
        if (
            self._timeline.virtual_size.height
            <= self._timeline.scrollable_content_region.height
        ):
            return
        self._timeline.scroll_end(animate=False)

    def _ensure_iteration(self, index: int, max_iterations: int | None) -> IterationState:
        state = self._iterations.get(index)
        if state is None:
            state = IterationState(index=index, max_iterations=max_iterations)
            self._iterations[index] = state
            card = SummaryCard(f"iter-{index}", f"Iteration {index}", "Starting", 3)
            detail = IterationDetail(index, id=f"iter-{index}")
            state.card = card
            state.detail = detail
            if self._summary_rail:
                if self._summary_placeholder is not None:
                    self._summary_placeholder.remove()
                    self._summary_placeholder = None
                self._summary_rail.append(card)
                if self._summary_rail.index in (None, 0):
                    self._summary_rail.index = len(self._summary_rail.children) - 1
            if self._summary_detail:
                self._summary_detail.mount(detail)
        return state

    def _update_iteration_card(self, state: IterationState) -> None:
        if state.card is None:
            return
        done = len(state.steps_done)
        total = state.steps_total
        status_parts = [f"{done}/{total} summaries"]
        if state.streaming:
            status_parts.insert(0, "streaming")
        state.card.set_status(" | ".join(status_parts))
        state.card.set_progress(done, total)
        state.card.set_spinner(state.streaming or done < total)
        state.card.remove_class("summary-active", "summary-pending", "summary-complete")
        if state.streaming:
            state.card.add_class("summary-active")
        elif total > 0 and done >= total:
            state.card.add_class("summary-complete")
        else:
            state.card.add_class("summary-pending")

    def _show_detail(self, detail_id: str) -> None:
        if self._summary_detail:
            self._summary_detail.current = detail_id

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        if isinstance(event.item, SummaryCard):
            self._show_detail(event.item.detail_id)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        if isinstance(event.item, SummaryCard):
            self._show_detail(event.item.detail_id)

    def on_header_update_message(self, message: HeaderUpdateMessage) -> None:
        if message.title is not None:
            self._title = message.title
        if message.iteration_label is not None:
            self._iteration_label = message.iteration_label
        if message.iteration_index is not None:
            self._current_iteration = message.iteration_index
        if message.max_iterations is not None:
            self._max_iterations = message.max_iterations
        if message.agent is not None:
            self._agent = message.agent
        if message.status is not None:
            self._status = message.status
        if message.streaming is not None:
            self._streaming = message.streaming
        self._update_header()

    def on_iteration_start_message(self, message: IterationStartMessage) -> None:
        self._current_iteration = message.index
        if message.max_iterations:
            self._max_iterations = message.max_iterations
        self._ensure_iteration(message.index, message.max_iterations)
        self._update_header()

    def on_config_update_message(self, message: ConfigUpdateMessage) -> None:
        self._session_state.config[message.key] = message.value
        if self._session_detail:
            self._session_detail.update_config(self._session_state.config)

    def on_summary_update_message(self, message: SummaryUpdateMessage) -> None:
        tag = message.tag.upper()
        if tag == "SYS" or message.iteration is None:
            self._session_state.sys_content = message.content
            if self._session_detail:
                self._session_detail.update_sys(message.content)
            return
        state = self._ensure_iteration(message.iteration, self._max_iterations)
        if tag == "PLAN":
            state.plan = message.content
            if state.detail:
                state.detail.update_plan(message.content)
        elif tag == "ACTIONS":
            state.actions = message.content
            if state.detail:
                state.detail.update_actions(message.content)
        elif tag == "REPORT":
            state.report = message.content
            if state.detail:
                state.detail.update_report(message.content)
        elif tag == "FINAL":
            state.final = message.content
            if state.detail:
                state.detail.update_final(message.content)
            state.steps_total = max(state.steps_total, 4)
        state.steps_done.add(tag)
        self._update_iteration_card(state)

    def on_timeline_message(self, message: TimelineMessage) -> None:
        if not self._timeline:
            return
        self._clear_stream_placeholder()
        if message.kind != "kv":
            self._kv_block = None
        if message.kind == "kv":
            if self._kv_block is None:
                self._kv_block = KeyValueTable("Config")
                self._timeline.mount(self._kv_block)
            self._kv_block.add_row(message.key or "", message.value or "")
            self._scroll_timeline_end()
            return
        widget: Static | Rule | PanelCard | StreamBlock | SectionHeader | SubsectionHeader
        if message.kind == "section":
            widget = SectionHeader(message.text or "")
        elif message.kind == "subsection":
            widget = SubsectionHeader(message.text or "")
        elif message.kind == "hr":
            widget = Rule(line_style="ascii")
        elif message.kind == "panel":
            title = message.title or ""
            tag = message.tag or ""
            label = f"{tag}: {title}" if title else tag
            widget = PanelCard(label, message.content or "")
        elif message.kind == "status":
            level = message.level or "info"
            widget = Static(
                message.text or "",
                classes=f"event-card {level}",
                markup=False,
            )
        elif message.kind == "box":
            widget = Static(message.text or "", classes="event-card", markup=False)
        else:
            widget = Static(message.text or "", classes="event-card", markup=False)
        self._timeline.mount(widget)
        self._scroll_timeline_end()

    def on_stream_block_start_message(self, message: StreamBlockStartMessage) -> None:
        if not self._timeline:
            return
        self._clear_stream_placeholder()
        self._kv_block = None
        label = message.title or "Stream"
        block = StreamBlock(message.channel, label)
        self._current_stream_block = block
        self._timeline.mount(block)
        if message.channel == "AI":
            self._streaming = True
            if self._current_iteration is not None:
                state = self._ensure_iteration(self._current_iteration, self._max_iterations)
                state.streaming = True
                self._update_iteration_card(state)
            self._update_header()
        self._scroll_timeline_end()

    def on_stream_block_end_message(self, message: StreamBlockEndMessage) -> None:
        if message.channel == "AI":
            self._streaming = False
            if self._current_iteration is not None:
                state = self._ensure_iteration(self._current_iteration, self._max_iterations)
                state.streaming = False
                self._update_iteration_card(state)
            self._update_header()
        self._current_stream_block = None

    def on_stream_line_message(self, message: StreamLineMessage) -> None:
        if not self._timeline:
            return
        self._clear_stream_placeholder()
        if self._current_stream_block is None:
            self._current_stream_block = StreamBlock(message.tag, "Output")
            self._timeline.mount(self._current_stream_block)
        self._current_stream_block.add_line(message.tag, message.line, TAG_WIDTH)
        self._scroll_timeline_end()

    async def on_prompt_request_message(self, message: PromptRequestMessage) -> None:
        request = message.request

        def finish(value: Any) -> None:
            request.result["value"] = value
            request.event.set()

        if request.kind == "choose":
            choose_modal = ChooseModal(request.header, request.options, request.default or 0)

            def handle_choose(result: int | None) -> None:
                if result is None:
                    result = request.default or 0
                finish(int(result))

            self.push_screen(choose_modal, callback=handle_choose)
        elif request.kind == "confirm":
            confirm_modal = ConfirmModal(request.header, bool(request.default))

            def handle_confirm(result: bool | None) -> None:
                if result is None:
                    result = bool(request.default)
                finish(bool(result))

            self.push_screen(confirm_modal, callback=handle_confirm)
        else:
            finish(request.default)


class TextualUI:
    def __init__(self, app: TextualApp):
        self._app = app
        self._current_iteration: int | None = None
        self._max_iterations: int | None = None
        self._final_message_mode = False
        self._final_lines: list[str] = []

    def title(self, text: str) -> None:
        self._post_header(title=text)

    def section(self, text: str) -> None:
        match = ITERATION_RE.search(text)
        if match:
            current = int(match.group(1))
            total = int(match.group(2))
            self._current_iteration = current
            self._max_iterations = total
            self._app.post_message(IterationStartMessage(current, total))
            self._post_header(
                iteration_label=f"{current}/{total}",
                iteration_index=current,
                max_iterations=total,
            )
        self._post_timeline(kind="section", text=text)

    def subsection(self, text: str) -> None:
        self._post_timeline(kind="subsection", text=text)

    def hr(self) -> None:
        self._post_timeline(kind="hr")

    def kv(self, key: str, value: str) -> None:
        self._app.post_message(ConfigUpdateMessage(key, value))
        self._post_timeline(kind="kv", key=key, value=value)
        if key.lower() == "agent":
            self._post_header(agent=value)

    def box(self, content: str) -> None:
        self._post_timeline(kind="box", text=content)

    def panel(self, tag: str, title: str, content: str) -> None:
        upper = tag.upper()
        if upper in SUMMARY_TAGS or upper == "SYS":
            summary_content = content
            if title:
                summary_content = (
                    f"## {title}\n\n{content}" if content else f"## {title}"
                )
            self._app.post_message(
                SummaryUpdateMessage(self._current_iteration, upper, title, summary_content)
            )
            return
        self._post_timeline(kind="panel", tag=tag, title=title, content=content)

    def startup_art(self) -> None:
        return

    def info(self, text: str) -> None:
        self._post_timeline(kind="status", text=text, level="info")

    def ok(self, text: str) -> None:
        self._post_timeline(kind="status", text=f"OK: {text}", level="ok")
        self._post_header(status="OK")

    def warn(self, text: str) -> None:
        self._post_timeline(kind="status", text=f"WARN: {text}", level="warn")
        self._post_header(status="WARN")

    def err(self, text: str) -> None:
        self._post_timeline(kind="status", text=f"ERROR: {text}", level="err")
        self._post_header(status="ERROR")

    def channel_header(self, channel: str, title: str = "") -> None:
        self._app.post_message(StreamBlockStartMessage(channel, title))
        if channel == "AI" and title == "Final message":
            self._final_message_mode = True
            self._final_lines = []

    def channel_footer(self, channel: str, title: str = "") -> None:
        _ = title
        self._app.post_message(StreamBlockEndMessage(channel, title))
        if self._final_message_mode:
            self._final_message_mode = False

    def stream_line(self, tag: str, line: str) -> None:
        if self._final_message_mode:
            self._final_lines.append(line)
            content = "\n".join(self._final_lines)
            self._app.post_message(
                SummaryUpdateMessage(self._current_iteration, "FINAL", "Final", content)
            )
        self._app.post_message(StreamLineMessage(tag, line))

    def stream_lines(self, tag: str, stream: TextIO) -> Iterator[str]:
        for line in stream:
            line = line.rstrip("\\n")
            self.stream_line(tag, line)
            yield line

    def choose(self, header: str, options: list[str], default: int = 0) -> int:
        request = PromptRequest(
            kind="choose",
            header=header,
            options=options,
            default=default,
            event=threading.Event(),
            result={},
        )
        self._app.post_message(PromptRequestMessage(request))
        request.event.wait()
        return int(request.result.get("value", default))

    def confirm(self, prompt: str, default: bool = False) -> bool:
        request = PromptRequest(
            kind="confirm",
            header=prompt,
            options=[],
            default=int(default),
            event=threading.Event(),
            result={},
        )
        self._app.post_message(PromptRequestMessage(request))
        request.event.wait()
        return bool(request.result.get("value", default))

    def can_prompt(self) -> bool:
        return True

    def _post_timeline(
        self,
        kind: str,
        text: str | None = None,
        key: str | None = None,
        value: str | None = None,
        tag: str | None = None,
        title: str | None = None,
        content: str | None = None,
        level: str | None = None,
    ) -> None:
        self._app.post_message(
            TimelineMessage(
                kind=kind,
                text=text,
                key=key,
                value=value,
                tag=tag,
                title=title,
                content=content,
                level=level,
            )
        )

    def _post_header(
        self,
        title: str | None = None,
        iteration_label: str | None = None,
        iteration_index: int | None = None,
        max_iterations: int | None = None,
        agent: str | None = None,
        status: str | None = None,
        streaming: bool | None = None,
    ) -> None:
        self._app.post_message(
            HeaderUpdateMessage(
                title=title,
                iteration_label=iteration_label,
                iteration_index=iteration_index,
                max_iterations=max_iterations,
                agent=agent,
                status=status,
                streaming=streaming,
            )
        )


def run_textual_app(runner: Callable[[UI], Any], mode_label: str) -> int:
    app = TextualApp(runner=runner, mode_label=mode_label)
    app.run()
    return app.exit_code
