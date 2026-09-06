"""Operator-authored context files read into the engineer's prompt (R10.8).

One loader for the files an operator writes by hand and the factory
reads verbatim. The first is ``scripts/kstrl/golden-patterns.md``: what a
good change looks like in this repository, stated before the run rather
than distilled after it.

TRUST. These files are trusted the way ``CLAUDE.md`` is trusted, which
``run_loop`` prepends verbatim (``kstrl/loop.py``). They are NOT passed
through the knowledge layer's injection filter
(``knowledge._is_injection_attempt``), and that is deliberate: the filter
exists because distilled facts are LLM output that a prior component's
agent could have influenced, whereas the operator authored this file.
Filtering it would mean the harness silently dropping instructions its
own operator wrote.

H3a. The delimiters and the truncation line in this module are label
glue, not instruction text: they name a block so the engineer can tell
where the operator's words start and stop, and they address no role.
Issue #303 records label glue as outside the enrolled-prompt set, with
the same treatment already given to the feedforward markers
(``=== CODEBASE CONTEXT (auto-generated) ===``), the retry-context
markers (``=== PREVIOUS ATTEMPT CONTEXT ===``) and the CLAUDE.md heading
in ``loop.py``. Nothing here is bound to a name ending in the enrolled
suffix, and nothing here is a sentence addressed to the engineer. Adding
a sentence that tells the engineer what to DO with the block would make
it a prompt body and would put it under H3.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: The label the golden-patterns block carries in the engineer's prompt.
GOLDEN_PATTERNS_HEADER = "GOLDEN PATTERNS (operator-authored)"

#: Character budget for the golden-patterns file. The feedforward
#: convention is tokens times four (``FeedforwardConfig.max_context_tokens``
#: is spent as ``* 4`` in ``build_feedforward_context``), so 6000
#: characters is about 1500 tokens.
GOLDEN_PATTERNS_MAX_CHARS = 6000


@dataclass(frozen=True)
class OperatorFile:
    """One operator-authored file and how it enters the prompt."""

    path: Path
    header: str
    max_chars: int


def resolve_operator_path(rel: str, worktree: Path, root: Path) -> Path:
    """Where to read an operator file from for one component.

    The worktree copy wins when it exists, because ``scripts/kstrl/`` is
    normally committed and each component worktree therefore carries the
    file at the revision the component branched from. The repo root is
    the fallback for a project that has not committed the file yet, and
    for ``use_worktrees=False`` runs where the two are the same
    directory.

    ``rel`` may be absolute: ``relative_to_root`` falls back to the
    absolute path string when a configured path cannot be relativized
    against the root. ``Path.__truediv__`` with an absolute right-hand
    side yields that absolute path, so both branches resolve to the
    configured file and nothing is silently read from the wrong tree.
    """
    candidate = worktree / rel
    if candidate.is_file():
        return candidate
    return root / rel


def load_operator_file(spec: OperatorFile) -> str:
    """The delimited block for the prompt, or "" when there is nothing to add.

    Returns "" when the file is absent, empty or whitespace-only, so an
    unedited scaffold costs no tokens. An unreadable file (a directory in
    its place, mode 000, bytes that are not UTF-8) also returns "" and
    logs a warning: a bad operator file must not fail a run, but it must
    not be silent either.

    Past ``spec.max_chars`` the text is cut at the last newline inside
    the budget and a ``[truncated: ...]`` line naming the file is
    appended before the closing delimiter, so the engineer is told the
    block is partial rather than reading a sentence that stops mid-word.
    When the budget window holds no newline at all the hard cut stands.
    """
    if not spec.path.exists():
        return ""
    try:
        text = spec.path.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        # ValueError alongside OSError: UnicodeDecodeError is a
        # ValueError and would escape a fail-closed `except OSError`.
        logger.warning("Could not read operator file %s: %s", spec.path, exc)
        return ""
    if not text.strip():
        return ""

    body = text
    notice: str | None = None
    if len(text) > spec.max_chars:
        window = text[: spec.max_chars]
        newline = window.rfind("\n")
        body = window[:newline] if newline > 0 else window
        notice = f"[truncated: {len(body)} of {len(text)} characters shown; shorten {spec.path}]"
        logger.warning(
            "%s is %d characters; %d shown, the rest is not in the prompt",
            spec.path,
            len(text),
            len(body),
        )

    lines = [f"=== {spec.header} ===", body.rstrip("\n")]
    if notice is not None:
        lines.append(notice)
    lines.append(f"=== END {spec.header} ===")
    return "\n".join(lines)
