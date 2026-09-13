from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from pipeline import db


def test_pipeline_run_timestamps_are_timezone_aware_utc(monkeypatch) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE pipeline_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            items_collected INTEGER DEFAULT 0,
            items_scored INTEGER DEFAULT 0,
            errors TEXT,
            duration_secs REAL
        )"""
    )
    monkeypatch.setattr(db, "_connection", conn)

    run_id = db.start_pipeline_run("daily")
    db.complete_pipeline_run(run_id, "completed")
    row = conn.execute(
        "SELECT started_at, completed_at FROM pipeline_runs WHERE id = ?", (run_id,)
    ).fetchone()

    for value in (row["started_at"], row["completed_at"]):
        parsed = datetime.fromisoformat(value)
        assert parsed.tzinfo is not None
        assert parsed.utcoffset() == timezone.utc.utcoffset(parsed)
