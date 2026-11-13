"""Apply CDC events onto an in-memory projection by seq order.

Base behavior: sort by seq ascending; upsert/delete into projection;
skip duplicate event_id. Out-of-order buffering is left for later topics.

Key-change topic (04-key-rename-migrate):

* Source may emit ``op == "rename"`` events carrying ``entity_key`` (old key)
  and ``new_entity_key`` (new key). They are NOT plain upsert/delete: treating
  them so would drop the entity's applied_seq lineage or leave both keys alive.
* Mutually exclusive stream policy ``key_change_policy``:
    - ``migrate``: atomically re-attach the old key's projection row to the new
      key. The row keeps its payload; lineage stays traceable via
      ``renamed_from`` / ``prev_applied_seq`` on the row plus a key-change
      trace record. The old key disappears from the snapshot entirely (never a
      tombstone, never dual-alive). If the migration is not allowed the whole
      event fails and both keys stay exactly as before the event (atomic: the
      projection dict is only mutated by the single move, never delete+insert).
    - ``reject``: projection untouched; the event is quarantined into the
      key-change trace with reason ``policy_reject``; the old key row stays.
* Conflict refinement (implemented consistently): if the new key already holds
  a LIVE projection row, the migration is conflict-rejected — the event fails,
  is quarantined with reason ``new_key_conflict``, and both keys are unchanged.
  (A tombstone at the new key does not block; the moved row overwrites it.)
"""

from __future__ import annotations

from typing import Any

RENAME_POLICIES = ("migrate", "reject")

# Reject reasons recorded on key-change traces.
REASON_POLICY_REJECT = "policy_reject"
REASON_NEW_KEY_CONFLICT = "new_key_conflict"
REASON_OLD_KEY_NOT_LIVE = "old_key_not_live"
REASON_SAME_KEY = "same_key"
REASON_MISSING_NEW_KEY = "missing_new_key"


def _lineage_of(row: dict[str, Any] | None) -> dict[str, Any]:
    """Carry rename lineage fields across later upsert/delete on the same key."""
    row = row or {}
    return {
        "renamed_from": row.get("renamed_from"),
        "prev_applied_seq": row.get("prev_applied_seq"),
    }


def apply_events(
    events: list[dict[str, Any]],
    *,
    known_event_ids: set[str] | None = None,
    key_change_policy: str = "migrate",
) -> dict[str, Any]:
    if key_change_policy not in RENAME_POLICIES:
        raise ValueError(f"unknown key_change_policy: {key_change_policy}")

    known = set(known_event_ids or ())
    projection: dict[str, dict[str, Any]] = {}
    applied_log: list[dict[str, Any]] = []
    key_change_traces: list[dict[str, Any]] = []
    counts = {
        "source_events": len(events),
        "applied_upsert": 0,
        "applied_delete": 0,
        "applied_rename": 0,
        "rejected_rename": 0,
        "skipped_duplicate": 0,
        "max_seq": 0,
    }

    ordered = sorted(events, key=lambda e: (int(e.get("seq", 0)), str(e.get("event_id", ""))))

    for ev in ordered:
        event_id = str(ev["event_id"])
        entity_key = str(ev["entity_key"])
        op = str(ev.get("op", "upsert"))
        seq = int(ev.get("seq", 0))
        event_time = str(ev.get("event_time", ""))
        counts["max_seq"] = max(counts["max_seq"], seq)

        if event_id in known:
            counts["skipped_duplicate"] += 1
            applied_log.append(
                {
                    "event_id": event_id,
                    "entity_key": entity_key,
                    "op": op,
                    "seq": seq,
                    "event_time": event_time,
                    "result": "skipped_duplicate",
                    "payload": ev.get("payload"),
                }
            )
            continue

        known.add(event_id)

        if op == "rename":
            result = _apply_rename(
                projection,
                key_change_traces,
                event_id=event_id,
                entity_key=entity_key,
                new_entity_key=str(ev.get("new_entity_key") or "").strip(),
                seq=seq,
                policy=key_change_policy,
            )
            counts[result] += 1
            applied_log.append(
                {
                    "event_id": event_id,
                    "entity_key": entity_key,
                    "op": op,
                    "seq": seq,
                    "event_time": event_time,
                    "result": result,
                    "payload": None,
                    "new_entity_key": str(ev.get("new_entity_key") or "").strip(),
                }
            )
            continue

        if op == "delete":
            projection[entity_key] = {
                "entity_key": entity_key,
                "payload": None,
                "applied_seq": seq,
                "applied_event_id": event_id,
                "deleted": True,
                **_lineage_of(projection.get(entity_key)),
            }
            counts["applied_delete"] += 1
            result = "applied_delete"
        else:
            projection[entity_key] = {
                "entity_key": entity_key,
                "payload": ev.get("payload") or {},
                "applied_seq": seq,
                "applied_event_id": event_id,
                "deleted": False,
                **_lineage_of(projection.get(entity_key)),
            }
            counts["applied_upsert"] += 1
            result = "applied_upsert"

        applied_log.append(
            {
                "event_id": event_id,
                "entity_key": entity_key,
                "op": op,
                "seq": seq,
                "event_time": event_time,
                "result": result,
                "payload": ev.get("payload"),
            }
        )

    alive = [r for r in projection.values() if not r["deleted"]]
    deleted = [r for r in projection.values() if r["deleted"]]
    counts["projected_alive"] = len(alive)
    counts["projected_deleted"] = len(deleted)
    return {
        "counts": counts,
        "projection": list(projection.values()),
        "applied_log": applied_log,
        "key_change_traces": key_change_traces,
    }


def _apply_rename(
    projection: dict[str, dict[str, Any]],
    traces: list[dict[str, Any]],
    *,
    event_id: str,
    entity_key: str,
    new_entity_key: str,
    seq: int,
    policy: str,
) -> str:
    """Handle one key-change event. Returns the count key to increment.

    Migration is a single atomic move of the existing row object (payload and
    lineage carried over) — never a delete followed by a fresh insert. Any
    rejection performs zero mutation, so both keys keep their pre-event state.
    """
    old_row = projection.get(entity_key)
    trace: dict[str, Any] = {
        "event_id": event_id,
        "seq": seq,
        "old_key": entity_key,
        "new_key": new_entity_key,
        "decision": "rejected",
        "reason": None,
        "prev_applied_seq": old_row["applied_seq"] if old_row and not old_row["deleted"] else None,
        "prev_applied_event_id": old_row["applied_event_id"]
        if old_row and not old_row["deleted"]
        else None,
    }

    def reject(reason: str) -> str:
        trace["reason"] = reason
        traces.append(trace)
        return "rejected_rename"

    if not new_entity_key:
        return reject(REASON_MISSING_NEW_KEY)
    if new_entity_key == entity_key:
        return reject(REASON_SAME_KEY)
    if policy == "reject":
        return reject(REASON_POLICY_REJECT)
    if old_row is None or old_row["deleted"]:
        return reject(REASON_OLD_KEY_NOT_LIVE)
    new_row = projection.get(new_entity_key)
    if new_row is not None and not new_row["deleted"]:
        # Conflict refinement: conflict-reject (chosen over merge-with-rules).
        return reject(REASON_NEW_KEY_CONFLICT)

    # Atomic re-attach: move the row under the new key, preserving payload and
    # recording lineage so applied_seq semantics stay traceable.
    moved = {
        "entity_key": new_entity_key,
        "payload": old_row["payload"],
        "applied_seq": seq,
        "applied_event_id": event_id,
        "deleted": False,
        "renamed_from": entity_key,
        "prev_applied_seq": old_row["applied_seq"],
    }
    del projection[entity_key]
    projection[new_entity_key] = moved

    trace["decision"] = "migrated"
    traces.append(trace)
    return "applied_rename"
