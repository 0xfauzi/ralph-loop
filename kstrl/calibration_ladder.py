"""What the autonomy ladder does about a calibration regression (R10.11).

``python -m kstrl.calibration compare`` measures the regression; this
module is the consequence. It sits beside ``kstrl.calibration`` rather
than inside it so the measurement half stays free of control-plane
imports, and because ``kstrl/calibration.py`` is close enough to the
repo's 800-line growth ratchet that a fifty-line helper would have to be
trimmed to fit rather than written plainly.

Advisory first, in two tiers. A regression always opens a
``calibration_drift`` inbox item when the ladder is enabled; it demotes
only when the operator has set
``[autonomy] demote_on_calibration_regression = true``. Every entry
threshold in the ladder is an unmeasured placeholder, so revoking
autonomy by default would act on a number nobody has measured yet.

The compare command's exit code is unchanged by anything here (0 pass,
1 regression) so existing CI usage is unaffected. The one exception is a
``kstrl.toml`` that will not load: that exits 2, the same code an
unreadable baseline gets, because "the config is broken" must not read
as "the ladder is off".
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from kstrl.autonomy import (
    AutonomyConfig,
    AutonomyError,
    AutonomyState,
    DemotionTrigger,
    apply_demotion,
)
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    from pathlib import Path

    from kstrl.calibration import Comparison

#: Printed when a regression was found and the ladder is switched off, so
#: the absence of an inbox item is a stated outcome rather than silence.
LADDER_DISABLED_LINE = "autonomy ladder disabled; regression recorded in the report only"


def _already_demoted_for(state: AutonomyState, new_baseline: str) -> bool:
    """Whether this exact baseline already cost a level.

    Deduped on the NEW baseline's timestamp rather than on a run id: the
    compare command is a human-invoked measurement that gets re-run - to
    read the report again, to show someone - and each re-run would
    otherwise take another level for one measurement. The timestamp is
    the identity of the thing measured; the invocation is not.
    """
    return any(
        record.direction == "demote"
        and record.trigger == DemotionTrigger.CALIBRATION_REGRESSION.label
        and record.evidence.get("new_baseline") == new_baseline
        for record in state.history
    )


def _open_drift_item(
    root_dir: Path,
    failures: list[str],
    new_baseline: str,
    evidence: dict[str, Any],
) -> None:
    """Open the advisory ``calibration_drift`` item, non-fatally.

    Honours ``[inbox] enabled`` exactly as the factory's demotion notice
    does. An inbox that an operator switched off must not be re-enabled
    by a second emitter, and a failed write must not change the compare
    command's exit code, which is the measurement's answer.
    """
    try:
        inbox_config = InboxConfig.load(root_dir)
        if not inbox_config.enabled:
            return
        Inbox(root_dir, inbox_config).add(
            ItemKind.CALIBRATION_DRIFT,
            f"Calibration regression: {len(failures)} failing threshold(s)",
            detail="\n".join(failures),
            run_id=f"calibration:{new_baseline}",
            dedupe_key=f"calibration:{new_baseline}",
            evidence=evidence,
        )
    except (OSError, ValueError) as exc:
        print(f"warning: inbox write failed (non-fatal): {exc}", file=sys.stderr)


def report_to_ladder(comparison: Comparison, root_dir: Path) -> int | None:
    """Fold a comparison into the ladder. Returns an exit-code override.

    ``None`` means "leave the compare command's own exit code alone",
    which is every path but one: a ``kstrl.toml`` that will not load
    returns 2. Reading an unloadable config as "autonomy disabled" would
    be a silent removal of the mechanism - the operator switched the
    ladder ON and a typo elsewhere in the file switched it off again.
    """
    try:
        config = AutonomyConfig.load(root_dir)
    except (ValueError, AutonomyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if comparison.passed:
        return None
    if not config.enabled:
        print(LADDER_DISABLED_LINE)
        return None

    failures = list(comparison.failures)
    new_baseline = comparison.new.timestamp
    evidence: dict[str, Any] = {
        "failures": failures,
        "new_baseline": new_baseline,
        "old": str(comparison.old.path),
        "new": str(comparison.new.path),
    }
    _open_drift_item(root_dir, failures, new_baseline, evidence)

    if not config.demote_on_calibration_regression:
        return None
    state = AutonomyState.load(root_dir)
    if _already_demoted_for(state, new_baseline):
        print(f"autonomy: baseline {new_baseline} already cost a level; not demoting again")
        return None
    apply_demotion(
        root_dir,
        DemotionTrigger.CALIBRATION_REGRESSION,
        failures[0],
        evidence=evidence,
        run_id=f"calibration:{new_baseline}",
        ui=PlainUI(no_color=True),
        state=state,
    )
    return None
