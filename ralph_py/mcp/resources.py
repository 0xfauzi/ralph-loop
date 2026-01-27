"""Resource resolution for ralph:// URIs."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pydantic.networks import AnyUrl

from mcp.server.lowlevel.helper_types import ReadResourceContents

from mcp import types

from ralph_py.mcp.schema import InvalidArgumentError, ValidationIssue


@dataclass(frozen=True)
class ResourceSpec:
    """Definition for a ralph:// resource."""

    name: str
    uri: str
    description: str
    relative_path: Path
    mime_type: str | None = None

    def to_resource(self) -> types.Resource:
        """Convert the spec into an MCP Resource definition."""
        return types.Resource(
            name=self.name,
            title=self.name.replace("_", " ").title(),
            uri=_as_url(self.uri),
            description=self.description,
            mimeType=self.mime_type,
        )


_RESOURCE_SPECS: tuple[ResourceSpec, ...] = (
    ResourceSpec(
        name="prompt",
        uri="ralph://prompt",
        description="Ralph prompt file",
        relative_path=Path("scripts/ralph/prompt.md"),
        mime_type="text/markdown",
    ),
    ResourceSpec(
        name="prd",
        uri="ralph://prd",
        description="Ralph PRD JSON",
        relative_path=Path("scripts/ralph/prd.json"),
        mime_type="application/json",
    ),
    ResourceSpec(
        name="progress",
        uri="ralph://progress",
        description="Ralph progress log",
        relative_path=Path("scripts/ralph/progress.txt"),
        mime_type="text/plain",
    ),
    ResourceSpec(
        name="codebase_map",
        uri="ralph://codebase_map",
        description="Ralph codebase map",
        relative_path=Path("scripts/ralph/codebase_map.md"),
        mime_type="text/markdown",
    ),
)

_RESOURCE_BY_URI = {spec.uri: spec for spec in _RESOURCE_SPECS}


def list_resources() -> list[types.Resource]:
    """Return the list of known ralph:// resources."""
    return [spec.to_resource() for spec in _RESOURCE_SPECS]


def get_resource_spec(uri: str) -> ResourceSpec:
    """Lookup a resource specification by URI."""
    spec = _RESOURCE_BY_URI.get(uri)
    if spec is None:
        raise InvalidArgumentError(
            "Unknown resource URI",
            [ValidationIssue("uri", f"Unknown resource URI: {uri}")],
        )
    return spec


def resolve_resource_path(root: Path, uri: str) -> Path:
    """Resolve a ralph:// URI into an absolute filesystem path."""
    root_path = _normalize_root(root)
    spec = get_resource_spec(uri)
    resolved = (root_path / spec.relative_path).resolve()
    if not _is_within_root(resolved, root_path):
        raise InvalidArgumentError(
            "Invalid resource URI",
            [ValidationIssue("uri", "Resource resolves outside root.")],
        )
    return resolved


def read_resource(root: Path, uri: str) -> list[ReadResourceContents]:
    """Read a ralph:// resource and return MCP text contents."""
    spec = get_resource_spec(uri)
    path = resolve_resource_path(root, uri)
    text = path.read_text()
    return [ReadResourceContents(content=text, mime_type=spec.mime_type)]


def _normalize_root(root: Path) -> Path:
    root_path = root.expanduser()
    if not root_path.is_absolute():
        raise InvalidArgumentError(
            "Invalid resource request",
            [ValidationIssue("root", "root must be an absolute path.")],
        )
    return root_path.resolve()


def _is_within_root(path: Path, root_path: Path) -> bool:
    try:
        path.relative_to(root_path)
    except ValueError:
        return False
    return True


def _as_url(value: str) -> AnyUrl:
    return cast(AnyUrl, value)
