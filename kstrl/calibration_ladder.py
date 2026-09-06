"""What the autonomy ladder does about a calibration regression (R10.11).

``python -m kstrl.calibration compare`` measures the regression; this
module is the consequence. It sits beside ``kstrl.calibration`` rather
than inside it so the measurement half stays free of control-plane
imports, and because folding it back in would not fit: measured, that is
752 lines plus this module's 202 of body plus the 8 import lines it does
not already have, so 962 against the repo's 800-line growth ratchet.

Advisory first, in two tiers. A regression always opens a
``calibration_drift`` inbox item when the ladder is enabled; it demotes
only when the operator has set
``[autonomy] demote_on_calibration_regression = true``. Every entry
threshold in the ladder is an unmeasured placeholder, so revoking
autonomy by default would act on a number nobody has measured yet.

The compare command's exit code is unchanged by anything here (0 pass,
1 regression) so existing CI usage is unaffected. The one exception is a
``kstrl.toml`` that will not load ON A REGRESSION: that exits 2, the
same code an unreadable baseline gets, because "the config is broken"
must not read as "the ladder is off". A PASSING comparison never reaches
the config at all - the ladder is not consulted, so there is nothing for
a broken file to be mis-read as, and turning every green run in a repo
with a typo'd ``kstrl.toml`` into an exit 2 would be a refusal nobody
asked for.
"""

from __future__ import annotations

import hashlib
import sys
from datetime import datetime
from typing import TYPE_CHECKING, Any

from kstrl.autonomy import (
    AutonomyConfig,
    AutonomyError,
    AutonomyState,
    DemotionTrigger,
    apply_demotion,
)
from kstrl.calibration import UNKNOWN_TIMESTAMP
from kstrl.inbox import Inbox, InboxConfig, ItemKind
from kstrl.statedir import ControlStateError
from kstrl.ui.plain import PlainUI

if TYPE_CHECKING:
    from pathlib import Path

    from kstrl.calibration import Comparison

#: Printed when a regression was found and the ladder is switched off, so
#: the absence of an inbox item is a stated outcome rather than silence.
#: The root is printed after it: a mistyped ``--root`` lands on a
#: directory with no ``kstrl.toml``, which loads as "disabled" and would
#: otherwise tell an operator the ladder is off when it is on.
LADDER_DISABLED_LINE = "autonomy ladder disabled; regression recorded in the report only"

#: The shape a captured baseline's ``timestamp`` has. ``save_report``
#: writes whatever its caller passed, so this is the CAPTURE's format,
#: checked rather than assumed: all 10 baselines committed under
#: ``tests/adversarial_fixtures/_results/`` match it and none does not.
#: Parsed rather than compared as text so a foreign file with some other
#: shape is read as "order unknown" instead of silently ordered by ASCII.
_TIMESTAMP_FORMAT = "%Y%m%d-%H%M%S"


def _parsed(timestamp: str) -> datetime | None:
    if timestamp == UNKNOWN_TIMESTAMP:
        return None
    try:
        return datetime.strptime(timestamp, _TIMESTAMP_FORMAT)
    except ValueError:
        return None


def _arguments_reversed(comparison: Comparison) -> bool:
    """Whether ``new`` is demonstrably OLDER than ``old``.

    ``compare <new> <old>`` reads every recovered fixture as newly
    missed, so a recovery reports as a regression. That was harmless
    while the command only printed; it costs a level now. False whenever
    the order cannot be established, so an unusual timestamp is never
    grounds for refusing to act.

    The advisory item is suppressed along with the demotion, deliberately:
    what this detects is that the comparison itself is inverted, so the
    ``calibration_drift`` item it would open describes a regression that
    did not happen. An operator reading the inbox would have no way to
    tell that row from a real one, and the printed refusal is on the
    surface the person who typed the command is looking at.
    """
    old_at = _parsed(comparison.old.timestamp)
    new_at = _parsed(comparison.new.timestamp)
    if old_at is None or new_at is None:
        return False
    return new_at < old_at


def _comparison_id(comparison: Comparison) -> str:
    """The identity of this PAIR of baselines.

    Two properties, both of which a key on the new baseline alone got
    wrong. A comparison is a pair, so ``compare old_A new`` and
    ``compare old_B new`` are different regressions and must not collapse
    onto one item. And ``Baseline.timestamp`` falls back to
    ``UNKNOWN_TIMESTAMP`` for a file with no ``timestamp`` key, which is
    a fill-in shared by every such file rather than an identity: keying
    on it made the first sentinel baseline the last one that could ever
    demote, because the next one deduped against it.

    So: the two timestamps when both are real, and otherwise a digest of
    the regression itself. The digest says in the key how it was derived,
    and it dedupes on what was MEASURED, which is the property the
    timestamps were standing in for.

    "What was measured" is the whole RATE TABLE, both sides of it, not
    the failure lines. A floor failure quotes only the new rate ("role
    'security' detection rate 0.60 is below its floor 0.80"), so two
    different old baselines whose difference sits in a role that does not
    fail produce byte-identical failure text - and a digest over that
    text alone put two different comparisons on one key, which is the
    same collision the sentinel caused, one field over.
    """
    old_ts = comparison.old.timestamp
    new_ts = comparison.new.timestamp
    if UNKNOWN_TIMESTAMP in (old_ts, new_ts):
        return f"sha256-{_measurement_digest(comparison)[:16]}"
    return f"{old_ts}:{new_ts}"


def _measurement_digest(comparison: Comparison) -> str:
    """A digest of both baselines' numbers, in the comparison's own order.

    The paths are deliberately not in it: the identity is the
    measurement, and the same pair of files re-read has to dedupe.
    """
    material = "\n".join(
        [
            *(
                f"role {delta.role} {delta.old_rate} -> {delta.new_rate}"
                for delta in comparison.role_deltas
            ),
            *(
                f"category {delta.role}/{delta.category} {delta.old_rate} -> {delta.new_rate}"
                for delta in comparison.category_deltas
            ),
            *comparison.failures,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _already_demoted_for(state: AutonomyState, comparison_id: str) -> bool:
    """Whether this exact comparison already cost a level.

    Deduped on the comparison rather than on a run id: the compare
    command is a human-invoked measurement that gets re-run - to read the
    report again, to show someone - and each re-run would otherwise take
    another level for one measurement. The pair is the identity of the
    thing measured; the invocation is not.
    """
    return any(
        record.direction == "demote"
        and record.trigger == DemotionTrigger.CALIBRATION_REGRESSION.label
        and record.evidence.get("comparison_id") == comparison_id
        for record in state.history
    )


def _open_drift_item(root_dir: Path, evidence: dict[str, Any]) -> None:
    """Open the advisory ``calibration_drift`` item, non-fatally.

    Honours ``[inbox] enabled`` exactly as the factory's demotion notice
    does. An inbox that an operator switched off must not be re-enabled
    by a second emitter, and a failed write must not change the compare
    command's exit code, which is the measurement's answer.

    The title, the dedupe key and the payload are all read off the one
    ``evidence`` dict rather than passed separately, so an item cannot
    describe the regression three ways. That holds only because the key
    is the identity of the comparison the payload came from, and because
    ``Inbox.add`` refreshes ``evidence`` alongside ``detail`` on a
    repeat: with either half missing, one item ends up carrying the title
    of one comparison and the numbers of another.
    """
    failures: list[str] = evidence["failures"]
    comparison_id: str = evidence["comparison_id"]
    try:
        inbox_config = InboxConfig.load(root_dir)
        if not inbox_config.enabled:
            return
        Inbox(root_dir, inbox_config).add(
            ItemKind.CALIBRATION_DRIFT,
            f"Calibration regression: {len(failures)} failing threshold(s)",
            detail="\n".join(failures),
            run_id=f"calibration:{comparison_id}",
            dedupe_key=f"calibration:{comparison_id}",
            evidence=evidence,
        )
    except (OSError, TypeError, ValueError, ControlStateError) as exc:
        # The callee's surface. ControlStateError is a RuntimeError, so
        # it escapes the (OSError, ValueError) pair the inbox sites were
        # written with, and Inbox._append takes the control lock on every
        # write. TypeError is InboxConfig.load's per-key cast, the same
        # one report_to_ladder catches on the ladder's config twenty-five
        # lines above; without it a valid document with one wrong VALUE
        # left a measurement command as a traceback.
        print(f"warning: inbox write failed (non-fatal): {exc}", file=sys.stderr)


def report_to_ladder(comparison: Comparison, root_dir: Path) -> int | None:
    """Fold a comparison into the ladder. Returns an exit-code override.

    ``None`` means "leave the compare command's own exit code alone",
    which is every path but one: a ``kstrl.toml`` that will not load
    returns 2. Reading an unloadable config as "autonomy disabled" would
    be a silent removal of the mechanism - the operator switched the
    ladder ON and a typo elsewhere in the file switched it off again.

    Everything AFTER the config resolves is reported and never changes
    the exit code: the regression is real whether or not the inbox took
    the item or the ladder took the level, and the exit code is the
    measurement's answer, not the bookkeeping's.
    """
    if comparison.passed:
        return None
    try:
        config = AutonomyConfig.load(root_dir)
    except (OSError, TypeError, ValueError, AutonomyError) as exc:
        # The callee's surface, not an enumeration of believed causes.
        # load_toml_section normalises a document fault to ConfigError (a
        # ValueError) and deliberately does NOT normalise OSError; a
        # per-key cast raises TypeError for a TOML date or array; and
        # __post_init__ raises AutonomyError, a RuntimeError, for a
        # max_level outside the ladder.
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not config.enabled:
        print(f"{LADDER_DISABLED_LINE} (root: {root_dir})")
        return None
    if _arguments_reversed(comparison):
        print(
            f"autonomy: new baseline {comparison.new.timestamp} is older than "
            f"{comparison.old.timestamp}; arguments look reversed, ladder not consulted"
        )
        return None

    failures = list(comparison.failures)
    comparison_id = _comparison_id(comparison)
    evidence: dict[str, Any] = {
        "failures": failures,
        "comparison_id": comparison_id,
        "new_baseline": comparison.new.timestamp,
        "old": str(comparison.old.path),
        "new": str(comparison.new.path),
    }
    _open_drift_item(root_dir, evidence)

    if not config.demote_on_calibration_regression:
        return None
    # Load / check-history / demote / save is a read-modify-write that no
    # single lock covers, so a factory demotion landing between the load
    # and the save is lost. NOT closed here: ``control_lock`` is the
    # obvious wrapper and it self-deadlocks, measured - it is a
    # ``flock`` on a fresh file description, and ``AutonomyState.save``
    # and ``Inbox._append`` each take it themselves, so an outer hold
    # blocks the inner one in the same process. Closing it means a
    # lock-holding variant of both callees and changing every caller
    # including the factory, which is a different change from this one.
    try:
        state = AutonomyState.load(root_dir)
        if _already_demoted_for(state, comparison_id):
            print(f"autonomy: comparison {comparison_id} already cost a level; not demoting again")
            return None
        apply_demotion(
            root_dir,
            DemotionTrigger.CALIBRATION_REGRESSION,
            failures[0],
            evidence=evidence,
            run_id=f"calibration:{comparison_id}",
            ui=PlainUI(no_color=True),
            state=state,
        )
    except (OSError, ValueError, ControlStateError) as exc:
        # The factory's caller absorbs these in an outer except Exception
        # and warns; this command has no such net, and a traceback out of
        # a measurement command is a third outcome nothing documents.
        print(f"error: autonomy demotion failed, level unchanged: {exc}", file=sys.stderr)
    return None
