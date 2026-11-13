"""Engine tests for the key-change (rename) topic.

Covers: atomic migration with lineage, no dual-alive, conflict-reject,
reject policy quarantine, failure atomicity, and count/trace cross-checks.
"""

from __future__ import annotations

from app.engines.project import apply_events


def _ev(event_id, key, op="upsert", seq=1, payload=None, new_key=None):
    e = {
        "event_id": event_id,
        "entity_key": key,
        "op": op,
        "seq": seq,
        "event_time": "2026-03-01T00:00:00Z",
        "payload": payload,
    }
    if new_key is not None:
        e["new_entity_key"] = new_key
    return e


def _rows_by_key(result):
    return {r["entity_key"]: r for r in result["projection"]}


def test_migrate_success_moves_row_and_preserves_lineage():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "A", seq=2, payload={"v": 2}),
        _ev("e3", "A", seq=3, new_key="B", op="rename"),
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)

    assert set(rows) == {"B"}, "old key must not survive migration (no dual-alive)"
    moved = rows["B"]
    assert moved["payload"] == {"v": 2}, "payload must be preserved, not reset"
    assert moved["applied_seq"] == 3
    assert moved["applied_event_id"] == "e3"
    assert moved["renamed_from"] == "A"
    assert moved["prev_applied_seq"] == 2, "pre-rename applied_seq stays traceable"
    assert not moved["deleted"]

    assert res["counts"]["applied_rename"] == 1
    assert res["counts"]["rejected_rename"] == 0
    assert res["counts"]["projected_alive"] == 1

    traces = res["key_change_traces"]
    assert len(traces) == 1
    t = traces[0]
    assert t["decision"] == "migrated"
    assert t["old_key"] == "A" and t["new_key"] == "B"
    assert t["prev_applied_seq"] == 2 and t["prev_applied_event_id"] == "e2"


def test_migrate_conflict_reject_keeps_both_keys_unchanged():
    events = [
        _ev("e1", "A", seq=1, payload={"v": "old"}),
        _ev("e2", "B", seq=2, payload={"v": "existing"}),
        _ev("e3", "A", seq=3, new_key="B", op="rename"),
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)

    # Atomic failure: both keys exactly as if the event never ran.
    assert rows["A"]["payload"] == {"v": "old"} and rows["A"]["applied_seq"] == 1
    assert rows["B"]["payload"] == {"v": "existing"} and rows["B"]["applied_seq"] == 2
    assert rows["B"]["renamed_from"] is None
    assert not rows["A"]["deleted"] and not rows["B"]["deleted"]

    assert res["counts"]["applied_rename"] == 0
    assert res["counts"]["rejected_rename"] == 1
    t = res["key_change_traces"][0]
    assert t["decision"] == "rejected" and t["reason"] == "new_key_conflict"
    assert t["prev_applied_seq"] == 1


def test_reject_policy_quarantines_and_leaves_projection_untouched():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "A", seq=2, new_key="B", op="rename"),
    ]
    res = apply_events(events, key_change_policy="reject")
    rows = _rows_by_key(res)

    assert set(rows) == {"A"}, "reject policy must not create the new key"
    assert rows["A"]["payload"] == {"v": 1} and rows["A"]["applied_seq"] == 1
    assert rows["A"]["renamed_from"] is None
    assert res["counts"]["applied_rename"] == 0
    assert res["counts"]["rejected_rename"] == 1
    t = res["key_change_traces"][0]
    assert t["decision"] == "rejected" and t["reason"] == "policy_reject"


def test_rename_of_deleted_key_is_rejected():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "A", op="delete", seq=2),
        _ev("e3", "A", seq=3, new_key="B", op="rename"),
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)

    assert set(rows) == {"A"}
    assert rows["A"]["deleted"], "tombstone must stay as it was"
    assert rows["A"]["applied_seq"] == 2
    t = res["key_change_traces"][0]
    assert t["decision"] == "rejected" and t["reason"] == "old_key_not_live"


def test_rename_missing_source_and_same_key_are_rejected():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "GHOST", seq=2, new_key="B", op="rename"),
        _ev("e3", "A", seq=3, new_key="A", op="rename"),
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)

    assert set(rows) == {"A"}
    assert rows["A"]["applied_seq"] == 1
    reasons = [t["reason"] for t in res["key_change_traces"]]
    assert reasons == ["old_key_not_live", "same_key"]
    assert res["counts"]["rejected_rename"] == 2


def test_lineage_survives_later_upsert_on_new_key():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "A", seq=2, new_key="B", op="rename"),
        _ev("e3", "B", seq=3, payload={"v": 3}),
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)

    assert set(rows) == {"B"}
    assert rows["B"]["payload"] == {"v": 3} and rows["B"]["applied_seq"] == 3
    assert rows["B"]["renamed_from"] == "A"
    assert rows["B"]["prev_applied_seq"] == 1


def test_duplicate_rename_event_is_skipped():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "A", seq=2, new_key="B", op="rename"),
    ]
    res = apply_events(events, known_event_ids={"e2"}, key_change_policy="migrate")
    rows = _rows_by_key(res)

    assert set(rows) == {"A"}
    assert res["counts"]["skipped_duplicate"] == 1
    assert res["counts"]["applied_rename"] == 0
    assert res["key_change_traces"] == []


def test_counts_traces_and_snapshot_key_set_cross_verify():
    events = [
        _ev("e1", "A", seq=1, payload={"v": 1}),
        _ev("e2", "C", seq=2, payload={"v": 9}),
        _ev("e3", "A", seq=3, new_key="B", op="rename"),  # migrates
        _ev("e4", "C", seq=4, new_key="B", op="rename"),  # conflict with migrated B
    ]
    res = apply_events(events, key_change_policy="migrate")
    rows = _rows_by_key(res)
    counts = res["counts"]
    traces = res["key_change_traces"]

    migrated = [t for t in traces if t["decision"] == "migrated"]
    rejected = [t for t in traces if t["decision"] == "rejected"]
    assert counts["applied_rename"] == len(migrated) == 1
    assert counts["rejected_rename"] == len(rejected) == 1
    alive_keys = {k for k, r in rows.items() if not r["deleted"]}
    for t in migrated:
        assert t["old_key"] not in alive_keys
        assert t["new_key"] in alive_keys
    assert alive_keys == {"B", "C"}
