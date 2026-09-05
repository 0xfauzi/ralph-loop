"""Init wizard logic layer (TUI surface D5) - Textual-free.

The wizard never generates content itself: scaffolding is run_init's
job (templates byte-identical, _create_if_missing non-destructive).
This module answers "what WOULD init touch" for the preview, exposes
the project-context detection, and offers exactly one write of its
own: substituting the STOCK commented [agent] lines that
DEFAULT_KSTRL_TOML ships - and refusing (False, no write) whenever
those exact lines are not found, because a user-edited file is never
something to guess inside.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from kstrl.init_cmd import (
    ScaffoldAction,
    _detect_project_context,
    classify_scaffold,
    gitignore_plan,
)

# The documented kstrl.toml [agent] type vocabulary (empty = auto).
AGENT_TYPES = ("", "claude-code", "claude-sdk", "codex")

# The stock commented lines DEFAULT_KSTRL_TOML scaffolds, keyed by the
# toml key they set. Substitution is line-targeted on these prefixes.
_AGENT_STOCK_PREFIXES = {
    "type": '# type = ""',
    "model": '# model = ""',
    "reasoning_effort": '# reasoning_effort = ""',
}


@dataclass(frozen=True)
class ScaffoldEntry:
    path: Path
    action: ScaffoldAction  # create = written; keep = left alone; append = added to
    #: For a scaffolded prompt that is an unedited OLDER kstrl template,
    #: the label it was shipped under (#286). "keep" is the honest action
    #: for it - the wizard's run_init call does leave it alone - but
    #: "exists - kept" on its own reads as "your scaffold is fine", which
    #: is precisely the belief #286 exists to correct. None for
    #: everything else, including a prompt the operator has edited.
    stale_label: str | None = None


def plan_scaffold(root: Path) -> list[ScaffoldEntry]:
    """The exact file set run_init touches, with the action for each.

    .gitignore is the one entry that is not create-or-keep: run_init
    appends its block to an existing file, so an existing .gitignore
    without that block is reported as ``append``. Saying "exists - kept"
    there would be a preview that does not match the write (#201).

    Files only. run_init has one non-file side effect, staging an
    untracked lockfile, and it reports that in its own transcript.
    """
    kstrl_dir = root / "scripts" / "kstrl"
    paths = [
        root / "kstrl.toml",
        kstrl_dir / "prompt.md",
        kstrl_dir / "prd.json",
        kstrl_dir / "progress.txt",
        kstrl_dir / "codebase_map.md",
        kstrl_dir / "golden-patterns.md",
        kstrl_dir / "understand_prompt.md",
        kstrl_dir / "feature_understand_prompt.md",
        root / "CLAUDE.md",
        root / "AGENTS.md",
    ]
    # One pass over the templates rather than a per-path lookup: the
    # only paths that can carry a label are the three under
    # scripts/kstrl/ that classify_scaffold already covers, and asking
    # it once keeps "which statuses are speakable" written in init_cmd
    # alone. Absent, current, edited and unknown all say nothing, the
    # same rule as the run-time warning.
    stale = {
        state.path: state.shipped_label
        for state in classify_scaffold(root)
        if state.status == "stale"
    }
    entries = [
        ScaffoldEntry(
            path=p,
            action="keep" if p.exists() else "create",
            stale_label=stale.get(p),
        )
        for p in paths
    ]
    entries.append(ScaffoldEntry(path=root / ".gitignore", action=gitignore_plan(root)))
    return entries


def detect_context(root: Path) -> dict[str, str]:
    """Public wrapper over init_cmd's project-context detection."""
    return _detect_project_context(root)


def apply_agent_settings(
    toml_path: Path,
    *,
    agent_type: str = "",
    model: str = "",
    reasoning: str = "",
) -> bool:
    """Substitute the stock commented [agent] lines with real values.

    All-or-nothing: if any requested key's stock line is missing (a
    user-edited or pre-existing file), NOTHING is written and False
    returns - the wizard reports it honestly instead of guessing.
    Empty values are skipped; all-empty is a no-op False.
    """
    wanted = {
        key: value
        for key, value in (
            ("type", agent_type),
            ("model", model),
            ("reasoning_effort", reasoning),
        )
        if value
    }
    if not wanted:
        return False
    try:
        content = toml_path.read_text(encoding="utf-8")
    except (OSError, ValueError):
        return False
    lines = content.splitlines(keepends=True)
    for key, value in wanted.items():
        prefix = _AGENT_STOCK_PREFIXES[key]
        for index, line in enumerate(lines):
            if line.startswith(prefix):
                ending = "\n" if line.endswith("\n") else ""
                encoded = json.dumps(value, ensure_ascii=False)
                lines[index] = f"{key} = {encoded}{ending}"
                break
        else:
            return False  # stock line missing: never guess
    toml_path.write_text("".join(lines))
    return True
