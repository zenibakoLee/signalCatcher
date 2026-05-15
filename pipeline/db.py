from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from pipeline.models import RawItem

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "signalcatcher.db"

_connection: sqlite3.Connection | None = None


def get_connection(readonly: bool = False) -> sqlite3.Connection:
    global _connection
    if _connection is not None and not readonly:
        return _connection

    path = str(DB_PATH)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(path, timeout=30)

    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")

    if not readonly:
        _connection = conn
    return conn


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = get_connection()
    conn.executescript(_SCHEMA)
    conn.commit()


def insert_raw_item(item: RawItem) -> int | None:
    conn = get_connection()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO raw_items
               (source, source_id, title, url, author, content_snippet, published_at, metadata)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                item.source,
                item.source_id,
                item.title,
                item.url,
                item.author,
                item.content_snippet,
                item.published_at.isoformat(),
                json.dumps(item.metadata) if item.metadata else None,
            ),
        )
        conn.commit()
        if cur.rowcount == 0:
            return None
        return cur.lastrowid
    except sqlite3.Error:
        conn.rollback()
        raise


def get_unscored_items(date: str) -> list[dict]:
    conn = get_connection()
    rows = conn.execute(
        """SELECT r.id, r.source, r.title, r.url, r.content_snippet, r.metadata
           FROM raw_items r
           LEFT JOIN scored_items s ON r.id = s.raw_item_id
           WHERE s.id IS NULL AND date(r.collected_at) = ?""",
        (date,),
    ).fetchall()
    return [dict(r) for r in rows]


def start_pipeline_run(run_type: str) -> int:
    conn = get_connection()
    from datetime import datetime

    cur = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES (?, ?)",
        (run_type, datetime.now().isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def complete_pipeline_run(
    run_id: int,
    status: str,
    items_collected: int = 0,
    items_scored: int = 0,
    errors: list[str] | None = None,
    duration_secs: float | None = None,
) -> None:
    conn = get_connection()
    from datetime import datetime

    conn.execute(
        """UPDATE pipeline_runs
           SET completed_at=?, status=?, items_collected=?, items_scored=?, errors=?, duration_secs=?
           WHERE id=?""",
        (
            datetime.now().isoformat(),
            status,
            items_collected,
            items_scored,
            json.dumps(errors) if errors else None,
            duration_secs,
            run_id,
        ),
    )
    conn.commit()


def get_active_keywords(conn: sqlite3.Connection | None = None) -> list[str]:
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT keyword FROM keywords WHERE status = 'active'"
    ).fetchall()
    return [r["keyword"] for r in rows]


def load_keywords_from_yaml(yaml_path: Path) -> None:
    import yaml

    conn = get_connection()
    with open(yaml_path) as f:
        data = yaml.safe_load(f)

    for category, keywords in data.get("keywords", {}).items():
        for kw in keywords:
            conn.execute(
                """INSERT OR IGNORE INTO keywords (keyword, category, added_by, status)
                   VALUES (?, ?, 'manual', 'active')""",
                (kw, category),
            )
    conn.commit()


_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT NOT NULL,
    source_id       TEXT NOT NULL,
    title           TEXT NOT NULL,
    url             TEXT,
    author          TEXT,
    content_snippet TEXT,
    published_at    TEXT NOT NULL,
    collected_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    metadata        TEXT,
    UNIQUE(source, source_id)
);
CREATE INDEX IF NOT EXISTS idx_raw_published ON raw_items(published_at);
CREATE INDEX IF NOT EXISTS idx_raw_collected ON raw_items(collected_at);

CREATE TABLE IF NOT EXISTS scored_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_item_id     INTEGER NOT NULL UNIQUE REFERENCES raw_items(id),
    score           INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
    score_reasoning TEXT,
    category        TEXT,
    scored_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    model_used      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scored_score ON scored_items(score DESC);

CREATE TABLE IF NOT EXISTS keyword_mentions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    source          TEXT NOT NULL,
    mention_date    TEXT NOT NULL,
    mention_count   INTEGER NOT NULL DEFAULT 0,
    sample_item_ids TEXT,
    UNIQUE(keyword, source, mention_date)
);
CREATE INDEX IF NOT EXISTS idx_km_keyword_date ON keyword_mentions(keyword, mention_date);

CREATE TABLE IF NOT EXISTS keyword_daily_aggregates (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    mention_date    TEXT NOT NULL,
    total_count     INTEGER NOT NULL DEFAULT 0,
    source_breakdown TEXT,
    UNIQUE(keyword, mention_date)
);
CREATE INDEX IF NOT EXISTS idx_kda_keyword_date ON keyword_daily_aggregates(keyword, mention_date);

CREATE TABLE IF NOT EXISTS trend_alerts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword         TEXT NOT NULL,
    alert_date      TEXT NOT NULL,
    z_score         REAL NOT NULL,
    severity        TEXT NOT NULL,
    moving_avg_7d   REAL,
    moving_avg_30d  REAL,
    std_dev_30d     REAL,
    today_count     INTEGER NOT NULL,
    llm_interpretation TEXT,
    delivered       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(keyword, alert_date)
);

CREATE TABLE IF NOT EXISTS digests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_date     TEXT NOT NULL UNIQUE,
    headline        TEXT NOT NULL,
    summary_md      TEXT NOT NULL,
    top_item_ids    TEXT,
    trend_alert_ids TEXT,
    model_used      TEXT NOT NULL,
    generated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    delivered       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS conference_briefings (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    conference_name     TEXT NOT NULL,
    conference_start    TEXT NOT NULL,
    conference_end      TEXT NOT NULL,
    briefing_type       TEXT NOT NULL,
    content_md          TEXT NOT NULL,
    expected_items      TEXT,
    silent_signals      TEXT,
    source_item_ids     TEXT,
    model_used          TEXT NOT NULL,
    generated_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    delivered           INTEGER NOT NULL DEFAULT 0,
    UNIQUE(conference_name, conference_start, briefing_type)
);

CREATE TABLE IF NOT EXISTS keywords (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword  TEXT NOT NULL UNIQUE,
    category TEXT,
    added_by TEXT NOT NULL DEFAULT 'manual',
    status   TEXT NOT NULL DEFAULT 'active',
    added_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);
CREATE INDEX IF NOT EXISTS idx_keywords_status ON keywords(status);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type        TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    items_collected INTEGER DEFAULT 0,
    items_scored    INTEGER DEFAULT 0,
    errors          TEXT,
    duration_secs   REAL
);
"""
