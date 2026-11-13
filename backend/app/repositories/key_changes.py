from __future__ import annotations

from typing import Any

import sqlite3


def insert_key_change_trace(
    conn: sqlite3.Connection,
    *,
    batch_id: int,
    stream_id: int,
    trace: dict[str, Any],
    created_at: str,
) -> None:
    conn.execute(
        """
        INSERT INTO key_change_traces(
            batch_id, stream_id, event_id, seq, old_key, new_key,
            decision, reason, prev_applied_seq, prev_applied_event_id, created_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            batch_id,
            stream_id,
            trace["event_id"],
            trace["seq"],
            trace["old_key"],
            trace["new_key"],
            trace["decision"],
            trace.get("reason"),
            trace.get("prev_applied_seq"),
            trace.get("prev_applied_event_id"),
            created_at,
        ),
    )


def list_key_change_traces(
    conn: sqlite3.Connection,
    *,
    batch_id: int | None = None,
    stream_id: int | None = None,
    decision: str | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM key_change_traces WHERE 1=1"
    params: list[Any] = []
    if batch_id is not None:
        sql += " AND batch_id=?"
        params.append(batch_id)
    if stream_id is not None:
        sql += " AND stream_id=?"
        params.append(stream_id)
    if decision is not None:
        sql += " AND decision=?"
        params.append(decision)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def decision_totals(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT decision, COUNT(*) c FROM key_change_traces GROUP BY decision"
    ).fetchall()
    out = {"migrated": 0, "rejected": 0}
    for r in rows:
        out[r["decision"]] = r["c"]
    return out
