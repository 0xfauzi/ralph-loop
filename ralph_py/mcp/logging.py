"""Logging helpers for MCP tool runs."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

_LOG_DIR = Path(".ralph") / "logs"
_SUMMARY_SUFFIX = ".summary.json"
_TOOL_NAME_RE = re.compile(r"[^a-zA-Z0-9_.-]+")


@dataclass(frozen=True)
class LogArtifacts:
    """Artifacts produced by an MCP tool run."""

    log_path: Path
    summary_path: Path
    summary_payload: dict[str, Any]


def resolve_log_dir(root: Path, log_dir: Path | None = None) -> Path:
    """Resolve the directory for MCP logs."""
    if log_dir is None:
        candidate = root / _LOG_DIR
    else:
        candidate = log_dir if log_dir.is_absolute() else root / log_dir
    return candidate


def write_log(
    *,
    root: Path,
    tool: str,
    summary: str,
    exit_code: int,
    log_text: str | Iterable[str],
    log_dir: Path | None = None,
    now: datetime | None = None,
) -> LogArtifacts:
    """Write a tool log and summary JSON for MCP responses."""
    resolved_dir = resolve_log_dir(root, log_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)

    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    safe_tool = _sanitize_tool_name(tool)
    stem = f"{timestamp}_{safe_tool}"
    log_path = resolved_dir / f"{stem}.log"
    summary_path = resolved_dir / f"{stem}{_SUMMARY_SUFFIX}"

    content = _normalize_log_text(log_text)
    log_path.write_text(content, encoding="utf-8")

    summary_payload: dict[str, Any] = {
        "tool": tool,
        "exit_code": exit_code,
        "summary": summary,
        "log_path": str(log_path),
    }
    summary_path.write_text(
        json.dumps(summary_payload, ensure_ascii=False),
        encoding="utf-8",
    )

    return LogArtifacts(
        log_path=log_path,
        summary_path=summary_path,
        summary_payload=summary_payload,
    )


def build_tool_payload(
    *,
    summary: str,
    exit_code: int,
    log_path: Path,
    changed_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build the tool response payload returned to MCP clients."""
    payload: dict[str, Any] = {
        "summary": summary,
        "exit_code": exit_code,
        "log_path": str(log_path),
    }
    if changed_files is not None:
        payload["changed_files"] = list(changed_files)
    return payload


def _sanitize_tool_name(tool: str) -> str:
    stripped = tool.strip()
    sanitized = _TOOL_NAME_RE.sub("_", stripped)
    return sanitized or "tool"


def _normalize_log_text(log_text: str | Iterable[str]) -> str:
    if isinstance(log_text, str):
        return log_text
    return "\n".join(str(line) for line in log_text)
