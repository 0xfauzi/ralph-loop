"""Git operations for Ralph."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ralph_py.ui.base import UI


def is_git_repo(path: Path | None = None) -> bool:
    """Check if path is inside a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except Exception:
        return False


def get_repo_root(path: Path | None = None) -> Path | None:
    """Get the root directory of the git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return Path(result.stdout.strip())
    except Exception:
        pass
    return None


def branch_exists(branch: str, cwd: Path | None = None) -> bool:
    """Check if a branch exists."""
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0


def checkout_branch(
    branch: str, ui: UI, cwd: Path | None = None, source: str | None = None
) -> bool:
    """Checkout or create a branch.

    Args:
        branch: Branch name to checkout/create
        ui: UI for output
        cwd: Working directory
        source: Optional source label (e.g. "from PRD", "from RALPH_BRANCH")

    Returns True on success, False on failure.
    """
    source_suffix = f" ({source})" if source else ""

    if branch_exists(branch, cwd):
        # Branch exists, checkout
        ui.info(f"Branch: checking out existing branch {branch}{source_suffix}")
        result = subprocess.run(
            ["git", "checkout", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    else:
        # Create new branch
        ui.info(f"Branch: creating branch {branch}{source_suffix}")
        result = subprocess.run(
            ["git", "checkout", "-b", branch],
            cwd=cwd,
            capture_output=True,
            text=True,
        )

    # Stream git output
    output = result.stdout + result.stderr
    for line in output.strip().splitlines():
        if line:
            ui.stream_line("GIT", line)

    return result.returncode == 0


def get_changed_files(cwd: Path | None = None) -> set[str]:
    """Get all changed files (staged, unstaged, and untracked).

    Returns paths relative to repo root.
    """
    files: set[str] = set()

    # Unstaged changes
    result = subprocess.run(
        ["git", "diff", "--name-only"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        files.update(line.strip() for line in result.stdout.splitlines() if line.strip())

    # Staged changes
    result = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        files.update(line.strip() for line in result.stdout.splitlines() if line.strip())

    # Untracked files
    result = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=cwd,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        files.update(line.strip() for line in result.stdout.splitlines() if line.strip())

    return files


def restore_file(file: str, cwd: Path | None = None) -> bool:
    """Restore a tracked file (staged and working tree)."""
    result = subprocess.run(
        ["git", "restore", "--staged", "--worktree", "--", file],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0


def delete_untracked(file: str, cwd: Path | None = None) -> bool:
    """Delete an untracked file."""
    try:
        path = Path(cwd or ".") / file
        if path.exists():
            if path.is_dir():
                import shutil
                shutil.rmtree(path)
            else:
                path.unlink()
        return True
    except Exception:
        return False


def is_file_tracked(file: str, cwd: Path | None = None) -> bool:
    """Check if a file is tracked by git."""
    result = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", file],
        cwd=cwd,
        capture_output=True,
    )
    return result.returncode == 0
