"""Chunk 1 (TUI rewrite): schema-v2 event model, sinks, run layout.

The load-bearing test here is golden parity: V1CompatSink fed stamped v2
events must produce byte-equivalent progress.jsonl lines (modulo ts) to
calling the real ProgressLog convenience methods directly. That parity
is what lets the whole migration keep .kstrl/progress.jsonl consumers
(ks status v1 arm, the Linear ProgressSink) untouched.
"""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

import pytest

from kstrl import events as ev
from kstrl.observability import ProgressLog, read_progress_events


def _sample_events() -> list[ev.Event]:
    """One instance of every registered concrete type, non-default payloads."""
    return [
        ev.RunStarted(project="proj", components=3),
        ev.ComponentStarted(component="comp-a"),
        ev.ComponentCompleted(component="comp-a", duration_seconds=12.34, iterations=4),
        ev.ComponentFailed(component="comp-b", error="boom"),
        ev.ComponentSkipped(component="comp-c", reason="operator stopped"),
        ev.CircuitBreakerTripped(component="comp-b", iterations=5, error="no progress"),
        ev.ComponentRetrying(component="comp-b", attempt=2, reason="verify failed"),
        ev.VerificationResultEvent(
            component="comp-a",
            passed=True,
            checks=("tests", "lint"),
            failures=(),
            duration_seconds=3.5,
        ),
        ev.ReviewResultEvent(
            component="comp-a",
            passed=False,
            mode="hard",
            fail_count=2,
            advisory_count=1,
            duration_seconds=60.0,
        ),
        ev.ComponentUsage(
            component="comp-a",
            phase="engineer",
            calls=3,
            known_calls=2,
            unreported_calls=1,
            input_tokens=100,
            output_tokens=50,
            cache_read_tokens=10,
            cache_creation_tokens=5,
            total_tokens=165,
            cost_usd=0.123456,
            duration_seconds=42.0,
        ),
        ev.BudgetExceeded(
            component="comp-a",
            total_tokens=100,
            max_total_tokens=50,
            coverage=({"ceiling": "max_cost_usd", "covered_calls": 8},),
        ),
        ev.BudgetCoverage(
            ceiling="max_cost_usd",
            axis="cost",
            calls=13,
            covered_calls=8,
            uncovered_calls=5,
            uncovered_tokens=193633,
            uncovered_roles=("review",),
            detail="cost coverage is PARTIAL",
        ),
        ev.ContractResult(tier=1, passed=False, breaker="comp-a", duration_seconds=9.9),
        ev.RunCompleted(completed=2, failed=1, skipped=0, duration_seconds=100.0),
        ev.MergePendingV1(component="comp-a", pr_url="http://pr/1", error="not confirmed"),
        ev.PhaseSkipped(component="comp-a", phase="security", reason="budget"),
        ev.DiffFetchFailed(component="comp-a", error="git failed"),
        ev.ReviewDivergence(
            component="comp-a",
            attempts=(1, 2, 3),
            lines_changed=(612, 1408, 2907),
            files_changed=(4, 6, 7),
            blocking_findings=(6, 1, 10),
            blocked=True,
        ),
        ev.AdversarialAgentSelected(
            phase="review",
            agent_source="config",
            identity="codex (gpt-5)",
            agent_type="codex",
            model="gpt-5",
            homogeneous=True,
        ),
        ev.RunPlan(
            components=({"id": "comp-a", "title": "A", "deps": []},),
            max_total_tokens=1000,
            max_adversarial_calls=10,
        ),
        ev.ComponentScopeResolved(
            component="comp-a",
            scope_source="component_prd",
            origin="scripts/kstrl/feature/comp-a/prd.json",
            allowed_paths=("src/", "tests/"),
            harness_paths=("scripts/kstrl/codebase_map.md",),
        ),
        ev.PhaseStarted(component="comp-a", phase="review", attempt=1),
        ev.PhaseCompleted(
            component="comp-a", phase="review", passed=True, detail="", duration_seconds=30.0
        ),
        ev.IterationStarted(component="comp-a", iteration=1, max_iterations=10),
        ev.IterationCompleted(
            component="comp-a", iteration=1, duration_seconds=20.0, completed=False, timed_out=False
        ),
        ev.WorkerHeartbeat(component="comp-a", pid=123, elapsed_seconds=45.0),
        ev.CheckpointRequested(component="comp-a", kind="checkpoint", question="Approve?"),
        ev.CheckpointResolved(
            component="comp-a", kind="checkpoint", decision="approved", decided_by="operator"
        ),
        ev.PrCreated(component="comp-a", pr_number=7, pr_url="http://pr/7"),
        ev.PrMerged(component="comp-a", pr_number=7, pr_url="http://pr/7"),
        ev.PrMergePending(component="comp-a", pr_url="http://pr/7", error="pending"),
        ev.DistillResult(component="comp-a", facts_written=3, duration_seconds=12.0),
        ev.FactUtilizationMeasured(
            component="comp-a",
            measured=True,
            injected=5,
            referenced=2,
            reason="",
            core_injected=2,
            core_referenced=2,
            dependency_injected=1,
            dependency_referenced=0,
            sibling_injected=2,
            sibling_referenced=0,
        ),
        ev.FindingRecorded(
            component="comp-a",
            phase="review",
            category="test_quality",
            severity="fail",
            location="a.py:10",
            explanation="weak assert",
            attempt=1,
        ),
        ev.SpecIssueRecorded(
            severity="blocker",
            kind="ambiguity",
            summary="Spec contradicts itself",
            location="spec.md:12",
            suggestion="pick one",
        ),
        ev.ArtifactWritten(
            component="comp-a", label="prd", path="scripts/kstrl/feature/comp-a/prd.json"
        ),
        ev.Log(severity="warn", kind="kv", key="Root", text="/tmp/x"),
        ev.AutonomyTransition(
            direction="demote",
            from_level=3,
            to_level=2,
            actor="system",
            trigger="policy_violation",
            reason="envelope breach",
        ),
        ev.AutonomyLevelApplied(
            level=1,
            label="L1 Supervised",
            flags=("merge gate: ON (human approves)",),
            overrides=("[factory] pause_before_pr_merge=False contradicts L1",),
        ),
        ev.JournalRepaired(detail="the preceding line was not newline-terminated"),
    ]


class TestRoundTrip:
    def test_every_registered_type_round_trips(self) -> None:
        for event in _sample_events():
            stamped = ev.EventBus(run_id="r1").emit(event)
            line = stamped.to_json_line()
            parsed = ev.parse_event_line(line)
            assert parsed is not None
            assert type(parsed) is type(event), line
            assert parsed.to_dict()["data"] == stamped.to_dict()["data"]
            assert parsed.run_id == "r1"
            assert parsed.seq == stamped.seq
            assert parsed.ts == pytest.approx(stamped.ts)

    def test_sample_covers_registry(self) -> None:
        """Every registered type except the UnknownEvent fallback is
        exercised by the round-trip test; a new event added without a
        sample here fails loudly."""
        sampled = {type(e).type for e in _sample_events()}
        registered = set(ev._REGISTRY) - {"unknown"}
        assert sampled == registered


class TestTolerantDecode:
    def test_unknown_event_name(self) -> None:
        obj = {
            "schema": 2,
            "event": "flux_capacitor",
            "ts": 1.0,
            "run_id": "r",
            "component": "c",
            "source": "orchestrator",
            "seq": 3,
            "data": {"x": 1},
        }
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.UnknownEvent)
        assert event.type_name == "flux_capacitor"
        assert event.to_dict() == obj  # lossless re-serialization
        assert event.component == "c"

    def test_extra_keys_ignored(self) -> None:
        obj = {"event": "component_failed", "data": {"error": "x", "novel_key": 1}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.ComponentFailed)
        assert event.error == "x"

    def test_missing_keys_default(self) -> None:
        event = ev.event_from_dict({"event": "component_completed", "data": {}})
        assert isinstance(event, ev.ComponentCompleted)
        assert event.duration_seconds == 0.0
        assert event.iterations == 0

    def test_mistyped_values_degrade_to_defaults(self) -> None:
        obj = {
            "event": "component_completed",
            "data": {"duration_seconds": "fast", "iterations": 3},
        }
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.ComponentCompleted)
        assert event.duration_seconds == 0.0  # mistyped -> default
        assert event.iterations == 3  # well-typed -> kept

    def test_bool_not_accepted_as_int(self) -> None:
        obj = {"event": "component_completed", "data": {"iterations": True}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.ComponentCompleted)
        assert event.iterations == 0

    def test_int_accepted_for_float_field(self) -> None:
        obj = {"event": "component_completed", "data": {"duration_seconds": 5}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.ComponentCompleted)
        assert event.duration_seconds == 5.0

    def test_utilization_payload_without_the_flag_is_unmeasured(
        self,
    ) -> None:
        """#191: `measured` gates the counts. A payload that omits it
        must decode as UNMEASURED, never as a measured zero - reading a
        default 0 as evidence would make the L2+ gate look permanently
        unsatisfiable."""
        obj = {"event": "fact_utilization_measured", "data": {"injected": 3}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.FactUtilizationMeasured)
        assert event.measured is False
        assert event.referenced == 0

    def test_mistyped_utilization_flag_degrades_to_unmeasured(self) -> None:
        """The flag degrades toward "no evidence", the safe direction."""
        obj = {"event": "fact_utilization_measured", "data": {"measured": "yes", "referenced": 4}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.FactUtilizationMeasured)
        assert event.measured is False

    def test_list_becomes_tuple(self) -> None:
        obj = {"event": "verification_result", "data": {"checks": ["a", "b"]}}
        event = ev.event_from_dict(obj)
        assert isinstance(event, ev.VerificationResultEvent)
        assert event.checks == ("a", "b")

    def test_non_dict_and_torn_lines(self) -> None:
        assert ev.parse_event_line("") is None
        assert ev.parse_event_line("   ") is None
        assert ev.parse_event_line("[1, 2]") is None
        assert ev.parse_event_line('{"event": "log", "data"') is None  # torn

    def test_mangled_envelope_never_raises(self) -> None:
        obj: dict[str, Any] = {
            "event": "log",
            "ts": "yesterday",
            "seq": "first",
            "run_id": 7,
            "component": None,
            "source": 3,
            "data": None,
        }
        event = ev.event_from_dict(obj)
        assert event.ts == 0.0
        assert event.seq == 0
        assert event.run_id == ""
        assert event.component == ""


class TestReadEvents:
    def test_missing_file(self, tmp_path: Path) -> None:
        assert ev.read_events(tmp_path / "nope.jsonl") == []

    def test_torn_tail_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "events.jsonl"
        good = ev.Log(text="hello").to_json_line()
        p.write_text(good + "\n" + good[: len(good) // 2])
        events = ev.read_events(p)
        assert len(events) == 1
        assert isinstance(events[0], ev.Log)

    def test_seeded_replay_through_jsonl_sink(self, tmp_path: Path) -> None:
        rng = random.Random(0)
        pool = _sample_events()
        chosen = [pool[rng.randrange(len(pool))] for _ in range(200)]
        sink = ev.JsonlSink(tmp_path / "events.jsonl")
        bus = ev.EventBus(sink, run_id="replay")
        for event in chosen:
            bus.emit(event)
        bus.close()
        back = ev.read_events(tmp_path / "events.jsonl")
        assert len(back) == len(chosen)
        assert [type(e) for e in back] == [type(e) for e in chosen]
        assert [e.seq for e in back] == list(range(1, len(chosen) + 1))


class TestEventBus:
    def test_stamps_envelope(self) -> None:
        bus = ev.EventBus(run_id="r9", source="worker", component="comp-x")
        stamped = bus.emit(ev.Log(text="hi"))
        assert stamped.run_id == "r9"
        assert stamped.source == "worker"
        assert stamped.component == "comp-x"
        assert stamped.seq == 1
        assert stamped.ts > 0

    def test_explicit_component_wins_over_bus_default(self) -> None:
        bus = ev.EventBus(component="bus-comp")
        stamped = bus.emit(ev.ComponentFailed(component="explicit", error="e"))
        assert stamped.component == "explicit"

    def test_sink_exception_is_isolated_and_counted(self) -> None:
        class Boom:
            def emit(self, event: ev.Event) -> None:
                raise RuntimeError("sink died")

            def close(self) -> None:
                raise RuntimeError("close died")

        received: list[ev.Event] = []
        bus = ev.EventBus(Boom(), ev.CallbackSink(received.append))
        bus.emit(ev.Log(text="x"))
        bus.close()
        assert len(received) == 1  # later sink still ran
        assert bus.dropped == 2  # one emit failure + one close failure

    def test_add_sink_late(self, tmp_path: Path) -> None:
        bus = ev.EventBus()
        bus.emit(ev.Log(text="before"))
        sink = ev.JsonlSink(tmp_path / "late.jsonl")
        bus.add_sink(sink)
        bus.emit(ev.Log(text="after"))
        bus.close()
        back = ev.read_events(tmp_path / "late.jsonl")
        assert [e.to_dict()["data"]["text"] for e in back] == ["after"]


def _strip_ts(event_dict: dict[str, Any]) -> dict[str, Any]:
    out = dict(event_dict)
    out.pop("ts", None)
    return out


class TestV1CompatGoldenParity:
    """V1CompatSink(ProgressLog) output == direct ProgressLog calls."""

    def test_named_methods_parity(self, tmp_path: Path) -> None:
        direct_path = tmp_path / "direct.jsonl"
        compat_path = tmp_path / "compat.jsonl"
        direct = ProgressLog(direct_path, run_id="run-1")
        compat = ProgressLog(compat_path, run_id="run-1")
        bus = ev.EventBus(ev.V1CompatSink(compat), run_id="run-1")

        direct.factory_started("proj", 3)
        bus.emit(ev.RunStarted(project="proj", components=3))

        direct.component_started("comp-a")
        bus.emit(ev.ComponentStarted(component="comp-a"))

        direct.component_completed("comp-a", 12.339, 4)
        bus.emit(ev.ComponentCompleted(component="comp-a", duration_seconds=12.339, iterations=4))

        direct.component_failed("comp-b", "boom")
        bus.emit(ev.ComponentFailed(component="comp-b", error="boom"))

        direct.circuit_breaker_tripped("comp-b", 5, "stall")
        bus.emit(ev.CircuitBreakerTripped(component="comp-b", iterations=5, error="stall"))

        direct.component_retrying("comp-b", 2, "verify failed")
        bus.emit(ev.ComponentRetrying(component="comp-b", attempt=2, reason="verify failed"))

        direct.verification_result("comp-a", True, ["tests"], [], 3.456)
        bus.emit(
            ev.VerificationResultEvent(
                component="comp-a",
                passed=True,
                checks=("tests",),
                failures=(),
                duration_seconds=3.456,
            )
        )

        direct.review_result("comp-a", False, "hard", 2, 1, 60.0)
        bus.emit(
            ev.ReviewResultEvent(
                component="comp-a",
                passed=False,
                mode="hard",
                fail_count=2,
                advisory_count=1,
                duration_seconds=60.0,
            )
        )

        # Mirrors UsageTotals.to_dict() verbatim, which is the documented
        # contract of ProgressLog.component_usage. R8 added token_calls
        # (calls that reported a TOKEN figure, as opposed to cost alone)
        # and the cost ceiling added cost_calls (its mirror), so both
        # sides of the parity carry both. Every reader of this payload
        # looks keys up defensively, so older files simply lack them.
        usage = {
            "calls": 3,
            "known_calls": 2,
            "token_calls": 1,
            "cost_calls": 2,
            "unreported_calls": 1,
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_read_tokens": 10,
            "cache_creation_tokens": 5,
            "total_tokens": 165,
            "cost_usd": 0.123456,
            "duration_seconds": 42.0,
        }
        direct.component_usage("comp-a", "engineer", dict(usage))
        bus.emit(ev.ComponentUsage(component="comp-a", phase="engineer", **usage))

        direct.budget_exceeded("comp-a", 100, 50)
        bus.emit(ev.BudgetExceeded(component="comp-a", total_tokens=100, max_total_tokens=50))

        coverage_kwargs: dict[str, Any] = {
            "ceiling": "max_cost_usd",
            "axis": "cost",
            "calls": 13,
            "covered_calls": 8,
            "uncovered_calls": 5,
            "uncovered_tokens": 193633,
            "detail": "cost coverage is PARTIAL",
        }
        direct.budget_coverage(uncovered_roles=["review"], **coverage_kwargs)
        bus.emit(
            ev.BudgetCoverage(
                uncovered_roles=("review",),
                **coverage_kwargs,
            )
        )

        direct.contract_result(1, False, "comp-a", 9.876)
        bus.emit(ev.ContractResult(tier=1, passed=False, breaker="comp-a", duration_seconds=9.876))

        direct.factory_completed(2, 1, 0, 100.0)
        bus.emit(ev.RunCompleted(completed=2, failed=1, skipped=0, duration_seconds=100.0))

        direct.emit("merge_pending", "comp-a", {"pr_url": "http://pr/1", "error": "not confirmed"})
        bus.emit(ev.MergePendingV1(component="comp-a", pr_url="http://pr/1", error="not confirmed"))

        direct.emit("phase_skipped", "comp-a", {"phase": "security", "reason": "budget"})
        bus.emit(ev.PhaseSkipped(component="comp-a", phase="security", reason="budget"))

        direct.emit("diff_fetch_failed", "comp-a", {"error": "git failed"})
        bus.emit(ev.DiffFetchFailed(component="comp-a", error="git failed"))

        direct.emit(
            "adversarial_agent_selected",
            data={
                "phase": "review",
                "source": "config",
                "identity": "codex (gpt-5)",
                "agent_type": "codex",
                "model": "gpt-5",
                "homogeneous": True,
            },
        )
        bus.emit(
            ev.AdversarialAgentSelected(
                phase="review",
                agent_source="config",
                identity="codex (gpt-5)",
                agent_type="codex",
                model="gpt-5",
                homogeneous=True,
            )
        )

        direct_lines = [_strip_ts(e) for e in read_progress_events(direct_path)]
        compat_lines = [_strip_ts(e) for e in read_progress_events(compat_path)]
        assert compat_lines == direct_lines
        assert len(direct_lines) == 17  # every v1-named event type exercised

    def test_v2_only_events_are_dropped(self, tmp_path: Path) -> None:
        compat_path = tmp_path / "compat.jsonl"
        bus = ev.EventBus(ev.V1CompatSink(ProgressLog(compat_path, run_id="r")), run_id="r")
        bus.emit(ev.RunPlan(components=({"id": "a", "title": "A", "deps": []},)))
        bus.emit(ev.PhaseStarted(component="a", phase="verify", attempt=1))
        bus.emit(ev.WorkerHeartbeat(component="a", pid=1, elapsed_seconds=1.0))
        bus.emit(ev.Log(text="narration"))
        bus.emit(ev.PrMerged(component="a", pr_number=1, pr_url="u"))
        bus.emit(ev.SpecIssueRecorded(severity="blocker", summary="s"))
        bus.emit(ev.ArtifactWritten(label="manifest", path="m.json"))
        assert read_progress_events(compat_path) == []

    def test_progress_sinks_still_fed(self, tmp_path: Path) -> None:
        """R7.4 ProgressSink observers (e.g. Linear) attached to the
        wrapped ProgressLog receive events emitted through the bus."""
        seen: list[dict[str, Any]] = []

        class Recorder:
            def handle_event(self, event: dict[str, Any]) -> None:
                seen.append(event)

        log = ProgressLog(tmp_path / "p.jsonl", run_id="r")
        log.attach_sink(Recorder())
        bus = ev.EventBus(ev.V1CompatSink(log), run_id="r")
        bus.emit(ev.ComponentStarted(component="comp-a"))
        bus.emit(ev.Log(text="dropped for v1"))
        assert [e["event"] for e in seen] == ["component_started"]


class TestRunPaths:
    def test_layout(self, tmp_path: Path) -> None:
        rp = ev.RunPaths.for_run(tmp_path, "run-42")
        assert rp.events_file == tmp_path / ".kstrl" / "runs" / "run-42" / "events.jsonl"
        assert rp.engineer_events("c1").name == "engineer.jsonl"
        assert rp.engineer_log("c1").parent == rp.component_dir("c1")
        assert rp.phase_log("c1", "review").name == "review.log"


class TestJsonlSink:
    def test_append_and_reopen(self, tmp_path: Path) -> None:
        p = tmp_path / "s.jsonl"
        sink = ev.JsonlSink(p)
        sink.emit(ev.EventBus().emit(ev.Log(text="one")))
        sink.close()
        sink2 = ev.JsonlSink(p)
        sink2.emit(ev.EventBus().emit(ev.Log(text="two")))
        sink2.close()
        texts = [json.loads(line)["data"]["text"] for line in p.read_text().splitlines()]
        assert texts == ["one", "two"]


class TestBothSinksCarryTheSameBudgetHalt:
    """The durable sink and the progress log must record the same facts.

    Found by a real factory run, not by this suite: `JsonlSink` writes
    the event via `to_json_line`, so it picked up `condition`/`ceilings`
    automatically, while `V1CompatSink` delegates to a `ProgressLog`
    method with a HAND-WRITTEN field list that was never updated. The
    durable events.jsonl carried both fields; progress.jsonl silently
    dropped them.

    Nothing asserted the two agreed, which is why a whole-field
    divergence survived a green suite. These tests fix that, and are
    written to fail for ANY future field added to one sink and not the
    other - not just this pair.
    """

    @staticmethod
    def _emit_both(
        tmp_path: Path,
        event: ev.BudgetExceeded,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        from kstrl.observability import ProgressLog

        durable_path = tmp_path / "events.jsonl"
        progress_path = tmp_path / "progress.jsonl"
        ev.JsonlSink(durable_path).emit(event)
        ev.V1CompatSink(ProgressLog(progress_path)).emit(event)

        def payload(path: Path) -> dict[str, Any]:
            for line in path.read_text().splitlines():
                if not line.strip():
                    continue
                row = json.loads(line)
                if row.get("event") == "budget_exceeded":
                    return dict(row["data"])
            raise AssertionError(f"no budget_exceeded line in {path}")

        return payload(durable_path), payload(progress_path)

    def test_a_breach_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        durable, progress = self._emit_both(
            tmp_path,
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=4017316,
                max_total_tokens=0,
                cost_usd=5.123713,
                max_cost_usd=5.0,
                ceiling="max_cost_usd",
                condition="breached",
                ceilings=("max_cost_usd",),
            ),
        )
        assert durable == progress

    def test_an_unenforceable_halt_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        """The multi-ceiling case is where a single-value field loses
        the most, so it is the one most worth pinning."""
        durable, progress = self._emit_both(
            tmp_path,
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=0,
                max_total_tokens=500,
                cost_usd=0.0,
                max_cost_usd=100.0,
                ceiling="max_total_tokens, max_cost_usd",
                condition="unenforceable",
                ceilings=("max_total_tokens", "max_cost_usd"),
            ),
        )
        assert durable == progress
        assert progress["ceilings"] == ["max_total_tokens", "max_cost_usd"]
        assert progress["condition"] == "unenforceable"

    def test_a_partially_covered_halt_reaches_both_sinks_identically(
        self,
        tmp_path: Path,
    ) -> None:
        """R8: the coverage a ceiling had when it halted is part of the
        halt record, on BOTH sinks. Payload is the measured run's."""
        durable, progress = self._emit_both(
            tmp_path,
            ev.BudgetExceeded(
                component="comp-a",
                total_tokens=26522034,
                max_total_tokens=0,
                cost_usd=28.7545,
                max_cost_usd=25.0,
                ceiling="max_cost_usd",
                condition="breached",
                ceilings=("max_cost_usd",),
                coverage=(
                    {
                        "ceiling": "max_cost_usd",
                        "axis": "cost",
                        "calls": 13,
                        "covered_calls": 8,
                        "uncovered_calls": 5,
                        "uncovered_tokens": 193633,
                        "uncovered_roles": ["review"],
                        "roles": [],
                    },
                ),
            ),
        )
        assert durable == progress
        assert progress["coverage"][0]["uncovered_roles"] == ["review"]

    def test_a_legacy_halt_payload_decodes_without_coverage(self) -> None:
        """events.jsonl is append-only: a payload written before the
        coverage field must still decode, to an empty tuple rather than
        to a fabricated claim of full coverage."""
        event = ev.event_from_dict(
            {
                "event": "budget_exceeded",
                "data": {
                    "total_tokens": 5,
                    "max_total_tokens": 10,
                    "cost_usd": 9.0,
                    "max_cost_usd": 8.0,
                    "ceiling": "max_cost_usd",
                },
            }
        )
        assert isinstance(event, ev.BudgetExceeded)
        assert event.coverage == ()
        assert event.ceiling == "max_cost_usd"

    def test_a_legacy_coverage_payload_decodes_with_defaults(self) -> None:
        event = ev.event_from_dict(
            {
                "event": "budget_coverage",
                "data": {"ceiling": "max_cost_usd"},
            }
        )
        assert isinstance(event, ev.BudgetCoverage)
        assert event.ceiling == "max_cost_usd"
        assert event.uncovered_roles == ()
        assert event.uncovered_tokens == 0

    def test_every_coverage_field_survives_the_progress_log(
        self,
        tmp_path: Path,
    ) -> None:
        """The same generic guard for the new run-scoped event."""
        import dataclasses

        from kstrl.observability import ProgressLog

        event = ev.BudgetCoverage(
            ceiling="max_cost_usd",
            axis="cost",
            calls=13,
            covered_calls=8,
            uncovered_calls=5,
            uncovered_tokens=193633,
            uncovered_roles=("review",),
            detail="cost coverage is PARTIAL",
        )
        durable_path = tmp_path / "events.jsonl"
        progress_path = tmp_path / "progress.jsonl"
        ev.JsonlSink(durable_path).emit(event)
        ev.V1CompatSink(ProgressLog(progress_path)).emit(event)

        def payload(path: Path) -> dict[str, Any]:
            for line in path.read_text().splitlines():
                row = json.loads(line)
                if row.get("event") == "budget_coverage":
                    return dict(row["data"])
            raise AssertionError(f"no budget_coverage line in {path}")

        durable, progress = payload(durable_path), payload(progress_path)
        assert durable == progress
        base = {f.name for f in dataclasses.fields(ev.Event)}
        payload_fields = {f.name for f in dataclasses.fields(event)} - base - {"type"}
        missing = payload_fields - progress.keys()
        assert not missing, (
            f"BudgetCoverage fields missing from progress.jsonl: {missing}. "
            "Add them to ProgressLog.budget_coverage and the V1CompatSink "
            "bridge."
        )

    def test_every_event_field_survives_the_progress_log(
        self,
        tmp_path: Path,
    ) -> None:
        """Generic guard: any field added to BudgetExceeded later must
        appear in BOTH sinks. This is the assertion whose absence let a
        two-field divergence ship."""
        import dataclasses

        event = ev.BudgetExceeded(
            component="comp-a",
            total_tokens=1,
            max_total_tokens=2,
            cost_usd=3.0,
            max_cost_usd=4.0,
            ceiling="max_cost_usd",
            condition="breached",
            ceilings=("max_cost_usd",),
            coverage=({"ceiling": "max_cost_usd", "covered_calls": 8},),
        )
        durable, progress = self._emit_both(tmp_path, event)

        base = {f.name for f in dataclasses.fields(ev.Event)}
        payload_fields = {f.name for f in dataclasses.fields(event)} - base - {"type"}
        missing = payload_fields - progress.keys()
        assert not missing, (
            f"BudgetExceeded fields missing from progress.jsonl: {missing}. "
            "Add them to ProgressLog.budget_exceeded and the V1CompatSink "
            "bridge."
        )
        assert payload_fields <= durable.keys()
