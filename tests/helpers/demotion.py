"""Shared fixtures for the R10.11 (#232) demotion-trigger tests.

Three test modules drive the same two seams - the calibration compare
command and the factory's health call - so the baselines, the config
writer, the inbox reader and the fake ``kstrl.health`` live here rather
than being copied three ways. The split itself is the 800-line ratchet:
one file carrying the applier, the compare emitter, the health seam and
the config surface crossed it, and a file that long has more than one
job in it.

No LLM anywhere: the baselines are built from synthetic per-run records
through ``calibration.build_report``, the same path a real capture uses.
"""

from __future__ import annotations

import io
import json
from collections.abc import Sequence
from dataclasses import dataclass
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
from importlib.util import spec_from_loader
from pathlib import Path
from types import ModuleType
from typing import Any

from kstrl import calibration
from kstrl.autonomy import AutonomyConfig
from kstrl.events import EventBus
from kstrl.factory import FactoryResult, _record_autonomy_outcome
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.ui.plain import PlainUI
from tests.spine_utils import component, make_manifest

OLD_TS = "20260901-000000"
OTHER_OLD_TS = "20260902-000000"
NEW_TS = "20260905-000000"

#: (role, fixture id, category, cwe) for a full 12-fixture capture. The
#: shape matters, not the content: two security fixtures missed in the
#: new baseline put security at 0.60, under its 0.80 floor and a 0.40
#: drop, and take two categories from 1.00 to 0.00. Four failures.
FIXTURES: tuple[tuple[str, str, str, str | None], ...] = (
    ("security", "sec-01-sqli", "injection", "CWE-89"),
    ("security", "sec-02-ssrf", "ssrf", "CWE-918"),
    ("security", "sec-03-auth", "auth", "CWE-287"),
    ("security", "sec-04-path", "path_traversal", "CWE-22"),
    ("security", "sec-05-secret", "secrets", "CWE-798"),
    ("reviewer", "rev-01-scope", "scope_creep", None),
    ("reviewer", "rev-02-tests", "test_quality", None),
    ("reviewer", "rev-03-error", "error_handling", None),
    ("architect", "spec-01-no-error-handling", "spec_issues", None),
    ("architect", "spec-02-ambiguous", "spec_issues", None),
    ("architect", "spec-03-contradiction", "spec_issues", None),
    ("architect_allowed_paths", "spec-04-paths", "allowed_paths", None),
)
MISSED_IN_NEW = frozenset({"sec-04-path", "sec-05-secret"})
#: A structurally DIFFERENT regression: the reviewer role rather than
#: security, so the failure lines differ from the set above.
OTHER_MISSED_IN_NEW = frozenset({"rev-01-scope", "rev-02-tests"})
#: Missed in an OLD baseline, in a role whose rate then IMPROVES. An
#: improvement is not a failure, so two olds differing only here produce
#: byte-identical failure lines and differ only in the rate table.
ARCHITECT_MISSED_IN_OLD = frozenset({"spec-01-no-error-handling"})
EXPECTED_FAILURES = 4


def _records(missed: frozenset[str]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for role, fixture_id, category, cwe in FIXTURES:
        for _ in range(3):
            out.append(
                {
                    "role": role,
                    "fixture_id": fixture_id,
                    "category": category,
                    "cwe": cwe,
                    "caught": fixture_id not in missed,
                    "error": False,
                    "detail": "synthetic",
                }
            )
    return out


def baseline(tmp_path: Path, *, missed: frozenset[str], timestamp: str) -> Path:
    """One baseline file, through the real build/save path."""
    return calibration.save_report(
        calibration.build_report(
            _records(missed), model="haiku", timestamp=timestamp, runs_per_fixture=3
        ),
        tmp_path / "baselines",
    )


def baseline_pair(tmp_path: Path, *, missed: frozenset[str]) -> tuple[Path, Path]:
    """Write an old/new baseline pair; ``missed`` regresses the new one."""
    return (
        baseline(tmp_path, missed=frozenset(), timestamp=OLD_TS),
        baseline(tmp_path, missed=missed, timestamp=NEW_TS),
    )


def without_timestamp(path: Path) -> Path:
    """A copy of a baseline with no ``timestamp`` key.

    ``load_baseline`` fills that in with ``UNKNOWN_TIMESTAMP``, which is
    what makes such a file a fill-in rather than an identity.
    """
    data = json.loads(path.read_text(encoding="utf-8"))
    del data["timestamp"]
    out = path.with_name(f"{path.stem}-no-timestamp.json")
    out.write_text(json.dumps(data), encoding="utf-8")
    return out


def write_config(
    tmp_path: Path,
    *,
    autonomy: bool = True,
    demote_on_calibration: bool | None = None,
    demote_on_health: bool | None = None,
    inbox: bool | None = None,
) -> None:
    lines = ["[autonomy]", f"enabled = {'true' if autonomy else 'false'}"]
    if demote_on_calibration is not None:
        lines.append(
            f"demote_on_calibration_regression = {'true' if demote_on_calibration else 'false'}"
        )
    if demote_on_health is not None:
        lines.append(f"demote_on_health_breach = {'true' if demote_on_health else 'false'}")
    if inbox is not None:
        lines.extend(["", "[inbox]", f"enabled = {'true' if inbox else 'false'}"])
    (tmp_path / "kstrl.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def inbox_items(tmp_path: Path, kind: ItemKind | None = None) -> list[Any]:
    found = Inbox(tmp_path, InboxConfig()).items()
    if kind is None:
        return list(found)
    return [item for item in found if item.kind is kind]


def make_ui() -> tuple[PlainUI, io.StringIO]:
    buffer = io.StringIO()
    return PlainUI(no_color=True, file=buffer), buffer


@dataclass(frozen=True)
class FrozenBreach:
    """The record #151 is contracted to supply, spelled as the issue and
    ``docs/dark-factory-roadmap.md`` spell it: FROZEN.

    The seam is driven with this rather than a ``SimpleNamespace`` so
    what these tests prove is that the mandated shape works. The static
    half of the same claim - that the ``_HealthBreach`` Protocol accepts
    a frozen dataclass, which the obvious spelling of a Protocol does NOT
    - is ``factory._health_breach_seam_accepts_the_contract``, checked by
    ``uv run mypy kstrl/ --strict`` because ``pyproject.toml`` scopes
    mypy to ``kstrl/`` and nothing in CI type-checks this file.
    """

    metric: str
    rule: str
    value: float
    limit: float
    window_runs: int


def make_breach(
    metric: str = "retry_rate",
    rule: str = "WE1: 1 point beyond 3 sigma",
    *,
    value: float = 0.4,
    window_runs: int = 8,
) -> FrozenBreach:
    return FrozenBreach(metric=metric, rule=rule, value=value, limit=0.3, window_runs=window_runs)


def fake_health(*breaches: object, with_function: bool = True) -> ModuleType:
    module = ModuleType("kstrl.health")
    if with_function:
        module.health_breaches = lambda root_dir: list(breaches)  # type: ignore[attr-defined]
    return module


class _BrokenHealthLoader(Loader):
    """A ``kstrl.health`` that exists and dies on its own dependency."""

    def create_module(self, spec: ModuleSpec) -> ModuleType | None:
        return None

    def exec_module(self, module: ModuleType) -> None:
        raise ModuleNotFoundError("No module named 'scipy'", name="scipy")


class BrokenHealthFinder(MetaPathFinder):
    """Puts the loader above on the real import machinery.

    Through ``sys.meta_path`` rather than by patching
    ``importlib.import_module``, so what the guard sees is what a real
    ``kstrl/health.py`` importing a missing dependency would raise.
    """

    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None = None,
        target: ModuleType | None = None,
    ) -> ModuleSpec | None:
        if fullname != "kstrl.health":
            return None
        return spec_from_loader(fullname, _BrokenHealthLoader())


def run_compare(tmp_path: Path, old: Path, new: Path) -> int:
    """``python -m kstrl.calibration compare`` against a project root."""
    return calibration.main(["compare", str(old), str(new), "--root", str(tmp_path)])


def run_outcome(tmp_path: Path, *, run_id: str = "run-1") -> str:
    """One factory run's outcome folded into the ladder; returns the UI text.

    ``autonomy_config`` is resolved here, once, exactly as
    ``_run_factory_locked`` resolves it at run start and threads it down.
    """
    plain_ui, buffer = make_ui()
    _record_autonomy_outcome(
        root_dir=tmp_path,
        manifest=make_manifest([component("comp-a")]),
        factory_result=FactoryResult(completed=["comp-a"]),
        autonomy_config=AutonomyConfig.load(tmp_path),
        bus=EventBus(),
        run_id=run_id,
        ui=plain_ui,
    )
    return buffer.getvalue()
