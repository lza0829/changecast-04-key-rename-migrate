from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from app.config import FIXTURES_DIR
from app.db import connect
from app.engines.project import RENAME_POLICIES, apply_events
from app.repositories import batches as batch_repo
from app.repositories import key_changes as key_change_repo
from app.repositories import projections as proj_repo
from app.repositories import streams as stream_repo


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _load_fixture(name: str) -> list[dict[str, Any]]:
    path = FIXTURES_DIR / name
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"fixture {name} must be a JSON array")
    return data


def dashboard_stats() -> dict[str, Any]:
    conn = connect()
    try:
        rename_totals = key_change_repo.decision_totals(conn)
        return {
            "stream_count": conn.execute("SELECT COUNT(*) c FROM project_streams").fetchone()["c"],
            "enabled_streams": conn.execute(
                "SELECT COUNT(*) c FROM project_streams WHERE enabled=1"
            ).fetchone()["c"],
            "batch_count": conn.execute("SELECT COUNT(*) c FROM project_batches").fetchone()["c"],
            "projection_alive": conn.execute(
                "SELECT COUNT(*) c FROM projection_rows WHERE deleted=0"
            ).fetchone()["c"],
            "projection_deleted": conn.execute(
                "SELECT COUNT(*) c FROM projection_rows WHERE deleted=1"
            ).fetchone()["c"],
            "ooo_buffered": conn.execute("SELECT COUNT(*) c FROM ooo_buffer").fetchone()["c"],
            "renames_migrated": rename_totals["migrated"],
            "renames_rejected": rename_totals["rejected"],
            "apply_order": conn.execute(
                "SELECT value FROM settings WHERE key='apply_order'"
            ).fetchone()["value"],
            "key_change_policy": _get_setting(conn, "key_change_policy", "migrate"),
        }
    finally:
        conn.close()


def list_streams() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return stream_repo.list_streams(conn)
    finally:
        conn.close()


def list_batches(limit: int = 50) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = batch_repo.list_batches(conn, limit=limit)
        streams = {s["id"]: s for s in stream_repo.list_streams(conn)}
        for r in rows:
            s = streams.get(r["stream_id"])
            r["stream_code"] = s["code"] if s else None
            r["stream_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def list_projections(stream_id: int | None = None, include_deleted: bool = True) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = proj_repo.list_projections(
            conn, stream_id=stream_id, include_deleted=include_deleted
        )
        streams = {s["id"]: s for s in stream_repo.list_streams(conn)}
        for r in rows:
            s = streams.get(r["stream_id"])
            r["stream_code"] = s["code"] if s else None
            r["stream_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def list_events(batch_id: int | None = None, stream_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return batch_repo.list_applied_events(conn, batch_id=batch_id, stream_id=stream_id)
    finally:
        conn.close()


def list_ooo_buffer(stream_id: int | None = None) -> list[dict[str, Any]]:
    conn = connect()
    try:
        return proj_repo.list_ooo_buffer(conn, stream_id=stream_id)
    finally:
        conn.close()


def _get_setting(conn, key: str, default: str | None = None) -> str | None:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def list_settings() -> dict[str, str]:
    conn = connect()
    try:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


# Settings the operator may switch at runtime. key_change_policy is mutually
# exclusive by construction: a single row holds exactly one active strategy.
EDITABLE_SETTINGS = {"key_change_policy": set(RENAME_POLICIES)}


def update_setting(key: str, value: str) -> dict[str, str]:
    allowed = EDITABLE_SETTINGS.get(key)
    if allowed is None:
        raise ValueError(f"setting {key!r} is read-only")
    if value not in allowed:
        raise ValueError(f"value {value!r} not in {sorted(allowed)}")
    conn = connect()
    try:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
        return {r["key"]: r["value"] for r in rows}
    finally:
        conn.close()


def list_key_changes(
    batch_id: int | None = None,
    stream_id: int | None = None,
    decision: str | None = None,
) -> list[dict[str, Any]]:
    conn = connect()
    try:
        rows = key_change_repo.list_key_change_traces(
            conn, batch_id=batch_id, stream_id=stream_id, decision=decision
        )
        streams = {s["id"]: s for s in stream_repo.list_streams(conn)}
        for r in rows:
            s = streams.get(r["stream_id"])
            r["stream_code"] = s["code"] if s else None
            r["stream_title"] = s["title"] if s else None
        return rows
    finally:
        conn.close()


def key_change_overview() -> dict[str, Any]:
    """Cross-verify rename counts, snapshot key sets, traces and batches.

    Checks:
    * batch_vs_trace — batch rename counters summed over all batches must equal
      the number of migrated / rejected trace records (both written from the
      same engine result).
    * old_key_retired — for the latest batch of each stream, every migrated
      trace's old key must be absent from the current ALIVE snapshot key set
      (no dual-alive after a successful migration).
    * new_key_present — for the latest batch of each stream, every migrated
      trace's new key must be present in the current snapshot key set.
    """
    conn = connect()
    try:
        totals = key_change_repo.decision_totals(conn)
        batch_sums = conn.execute(
            "SELECT COALESCE(SUM(applied_rename),0) m, COALESCE(SUM(rejected_rename),0) r "
            "FROM project_batches"
        ).fetchone()
        latest_batches = [
            r["id"]
            for r in conn.execute(
                "SELECT MAX(id) id FROM project_batches GROUP BY stream_id"
            ).fetchall()
        ]
        migrated_latest: list[dict[str, Any]] = []
        if latest_batches:
            marks = ",".join("?" for _ in latest_batches)
            migrated_latest = [
                dict(r)
                for r in conn.execute(
                    f"SELECT * FROM key_change_traces WHERE decision='migrated' "
                    f"AND batch_id IN ({marks})",
                    latest_batches,
                ).fetchall()
            ]
        alive_keys = {
            (r["stream_id"], r["entity_key"])
            for r in conn.execute(
                "SELECT stream_id, entity_key FROM projection_rows WHERE deleted=0"
            ).fetchall()
        }
        present_keys = {
            (r["stream_id"], r["entity_key"])
            for r in conn.execute(
                "SELECT stream_id, entity_key FROM projection_rows"
            ).fetchall()
        }
        old_key_violations = sorted(
            {t["old_key"] for t in migrated_latest if (t["stream_id"], t["old_key"]) in alive_keys}
        )
        new_key_violations = sorted(
            {
                t["new_key"]
                for t in migrated_latest
                if (t["stream_id"], t["new_key"]) not in present_keys
            }
        )
        checks = [
            {
                "key": "batch_vs_trace",
                "ok": batch_sums["m"] == totals["migrated"]
                and batch_sums["r"] == totals["rejected"],
                "detail": f"批次累计 迁移 {batch_sums['m']} / 拒绝 {batch_sums['r']} "
                f"↔ 留痕 迁移 {totals['migrated']} / 拒绝 {totals['rejected']}",
            },
            {
                "key": "old_key_retired",
                "ok": not old_key_violations,
                "detail": "迁移后旧键不出现在存活快照"
                if not old_key_violations
                else f"旧键仍存活: {', '.join(old_key_violations)}",
            },
            {
                "key": "new_key_present",
                "ok": not new_key_violations,
                "detail": "迁移后新键在快照键集合内"
                if not new_key_violations
                else f"新键缺失: {', '.join(new_key_violations)}",
            },
        ]
        return {
            "policy": _get_setting(conn, "key_change_policy", "migrate"),
            "migrated_total": totals["migrated"],
            "rejected_total": totals["rejected"],
            "batch_applied_rename_sum": batch_sums["m"],
            "batch_rejected_rename_sum": batch_sums["r"],
            "lineage_rows": conn.execute(
                "SELECT COUNT(*) c FROM projection_rows WHERE renamed_from IS NOT NULL"
            ).fetchone()["c"],
            "checks": checks,
        }
    finally:
        conn.close()


def run_project(stream_id: int, note: str | None = None) -> dict[str, Any]:
    """Load CDC fixture, apply by seq, replace projection for the stream."""
    conn = connect()
    try:
        stream = stream_repo.get_stream(conn, stream_id)
        if not stream:
            raise ValueError("stream not found")
        if not stream["enabled"]:
            raise ValueError("stream disabled")

        events = _load_fixture(stream["fixture"])
        key_change_policy = _get_setting(conn, "key_change_policy", "migrate")
        # Base 0-1: each run rebuilds projection from the full fixture by seq.
        # Incremental watermark / cross-run idempotency left for later topics.
        result = apply_events(events, known_event_ids=set(), key_change_policy=key_change_policy)
        now = _now()
        seq = conn.execute("SELECT COUNT(*) c FROM project_batches").fetchone()["c"] + 1
        batch_no = f"PB-{now[:10].replace('-', '')}-{seq:04d}"
        batch_id = batch_repo.insert_batch(
            conn,
            batch_no=batch_no,
            stream_id=stream_id,
            status="completed",
            counts=result["counts"],
            note=note,
            created_at=now,
        )
        proj_repo.replace_stream_projection(
            conn, stream_id=stream_id, rows=result["projection"], updated_at=now
        )
        for ev in result["applied_log"]:
            batch_repo.insert_applied_event(
                conn, batch_id=batch_id, stream_id=stream_id, event=ev, created_at=now
            )
        for trace in result["key_change_traces"]:
            key_change_repo.insert_key_change_trace(
                conn, batch_id=batch_id, stream_id=stream_id, trace=trace, created_at=now
            )
        stream_repo.set_watermark(conn, stream_id, result["counts"]["max_seq"])
        conn.commit()

        batch = batch_repo.get_batch(conn, batch_id)
        assert batch is not None
        batch["stream_code"] = stream["code"]
        batch["stream_title"] = stream["title"]
        batch["apply_order"] = "by_seq"
        batch["key_change_policy"] = key_change_policy
        batch["counts"] = result["counts"]
        batch["projection_preview"] = proj_repo.list_projections(conn, stream_id=stream_id)[:20]
        batch["event_preview"] = batch_repo.list_applied_events(conn, batch_id=batch_id, limit=20)
        batch["key_change_traces"] = key_change_repo.list_key_change_traces(
            conn, batch_id=batch_id
        )
        return batch
    finally:
        conn.close()
