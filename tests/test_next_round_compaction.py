# SPDX-License-Identifier: Apache-2.0
"""Compaction is bounded native progress, never substitute document evidence."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import test_next_round_source_profile as source
import capture_nr03_app_server as capture
from check_codex_acceptance import EvidenceError
from nr03_source_fixtures import encode

assess, files, rebind, source_case = source.assess, source.files, source.rebind, source.source_case


def compacted(case):
    fixture = deepcopy(case[0])
    rows = [json.loads(line) for line in fixture["raw"].splitlines()]
    turn = next(r for r in rows if r.get("method") == "turn/started")["params"]
    pair = [dict(method=method, params=dict(threadId=turn["threadId"], turnId=turn["turn"]["id"],
            item=dict(type="contextCompaction", id="synthetic-compaction"), **{clock: value}))
            for method, clock, value in [("item/started", "startedAtMs", 1), ("item/completed", "completedAtMs", 2)]]
    rows[-1:-1] = pair
    fixture["raw"] = encode(rows)
    rebind(fixture)
    return fixture, case[1]


def test_compaction_preserves_original_call_and_delivery_requirements(source_case):
    original = assess(source_case)
    qualified = compacted(source_case)
    assert set(assess(qualified)) == set(original)
    bad = deepcopy(qualified[0])
    rows = [json.loads(line) for line in bad["raw"].splitlines()]
    rows.pop(next(n for n, r in enumerate(rows) if r.get("method") == "item/completed"
                  and r["params"]["item"]["type"] == "mcpToolCall"))
    bad["raw"] = encode(rows)
    rebind(bad)
    with pytest.raises(EvidenceError):
        assess((bad, qualified[1]))


@pytest.mark.parametrize("fault", ["missing_start", "missing_end", "duplicate_start", "duplicate_end",
    "foreign_thread", "foreign_turn", "different_id", "clock", "bool_clock", "extra_field", "mcp_identity"])
def test_compaction_refuses_incomplete_or_contradictory_lifecycle(source_case, fault):
    fixture, producer = compacted(source_case)
    rows = [json.loads(line) for line in fixture["raw"].splitlines()]
    start, end = rows[-3], rows[-2]
    if fault == "missing_start":
        rows.remove(start)
    elif fault == "missing_end":
        rows.remove(end)
    elif fault == "duplicate_start":
        rows.insert(-2, deepcopy(start))
    elif fault == "duplicate_end":
        rows.insert(-1, deepcopy(end))
    elif fault in {"foreign_thread", "foreign_turn"}:
        end["params"]["threadId" if fault == "foreign_thread" else "turnId"] = "foreign"
    elif fault == "different_id":
        end["params"]["item"]["id"] = "different"
    elif fault in {"clock", "bool_clock"}:
        end["params"]["completedAtMs"] = 0 if fault == "clock" else True
    elif fault == "extra_field":
        start["params"]["item"]["result"] = "not evidence"
    elif fault == "mcp_identity":
        ident = next(r["params"]["item"]["id"] for r in rows if r.get("method") == "item/started"
                     and r["params"]["item"]["type"] == "mcpToolCall")
        start["params"]["item"]["id"] = end["params"]["item"]["id"] = ident
    fixture["raw"] = encode(rows)
    rebind(fixture)
    with pytest.raises(EvidenceError):
        assess((fixture, producer))
    assert len(assess(compacted(source_case))) == 5


def timed_stream(monkeypatch, scheduled):
    clock = SimpleNamespace(now=0)
    monkeypatch.setattr(capture.time, "monotonic", lambda: clock.now)

    class ScheduledQueue:
        def get(self, timeout):
            if scheduled and scheduled[0][0] <= clock.now + timeout:
                clock.now, row = scheduled.pop(0)
                return encode([row]).encode()
            clock.now += timeout
            raise capture.queue.Empty

    stream = object.__new__(capture.StdioCapture)
    stream.failure = stream.compaction_deadline = None
    stream.cancel_check = lambda: None
    stream.notifications, stream.queue = [], ScheduledQueue()
    return stream, clock


def progress(method, kind="contextCompaction"):
    return dict(method=method, params=dict(item=dict(type=kind, id="synthetic")))


def test_long_compaction_completes_and_normal_timeout_is_restored(monkeypatch):
    stream, clock = timed_stream(monkeypatch, [(0, progress("item/started")),
                                             (120, progress("item/completed"))])
    stream.receive()
    assert stream.receive()["method"] == "item/completed" and clock.now == 120
    with pytest.raises(EvidenceError, match="source transport timed out"):
        stream.receive()
    assert 180 <= clock.now <= 180.1


def test_compaction_deadline_is_bounded_despite_other_progress(monkeypatch):
    stream, clock = timed_stream(monkeypatch, [(0, progress("item/started")),
        *[(n, dict(method="thread/tokenUsage/updated", params={})) for n in (50, 100, 150, 200, 250)]])
    for _ in range(6):
        stream.receive()
    with pytest.raises(EvidenceError, match="source transport timed out"):
        stream.receive()
    assert 300 <= clock.now <= 300.1


def test_cancellation_remains_responsive_during_compaction(monkeypatch):
    stream, clock = timed_stream(monkeypatch, [(0, progress("item/started"))])
    stream.receive()
    def cancel():
        if clock.now >= 2:
            raise EvidenceError("synthetic cancellation")
    stream.cancel_check = cancel
    with pytest.raises(EvidenceError, match="synthetic cancellation"):
        stream.receive()
    assert 2 <= clock.now <= 2.1
