from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
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
    _migrate_scored_items(conn)
    _migrate_theses(conn)
    conn.commit()


def _migrate_scored_items(conn: sqlite3.Connection) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(scored_items)").fetchall()
    }
    if "title_ko" not in cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN title_ko TEXT")
    if "related_tickers" not in cols:
        conn.execute("ALTER TABLE scored_items ADD COLUMN related_tickers TEXT")


def _migrate_theses(conn: sqlite3.Connection) -> None:
    cols = {
        row[1]
        for row in conn.execute("PRAGMA table_info(investment_theses)").fetchall()
    }
    if not cols:  # 테이블 미생성 (구버전) — 스키마가 생성함
        return
    if "depth_layer" not in cols:
        conn.execute("ALTER TABLE investment_theses ADD COLUMN depth_layer INTEGER")
    if "pricing_status" not in cols:
        conn.execute("ALTER TABLE investment_theses ADD COLUMN pricing_status TEXT")


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


def _kst_date_to_utc_range(date_str: str) -> tuple[str, str]:
    """Convert a KST date string (YYYY-MM-DD) to UTC start/end timestamps."""
    KST = timezone(timedelta(hours=9))
    parts = [int(p) for p in date_str.split("-")]
    kst_start = datetime(parts[0], parts[1], parts[2], tzinfo=KST)
    utc_start = kst_start.astimezone(timezone.utc)
    utc_end = utc_start + timedelta(days=1)
    return utc_start.strftime("%Y-%m-%dT%H:%M:%S"), utc_end.strftime("%Y-%m-%dT%H:%M:%S")


def get_unscored_items(date: str) -> list[dict]:
    conn = get_connection()
    utc_start, utc_end = _kst_date_to_utc_range(date)
    rows = conn.execute(
        """SELECT r.id, r.source, r.title, r.url, r.content_snippet, r.metadata
           FROM raw_items r
           LEFT JOIN scored_items s ON r.id = s.raw_item_id
           WHERE s.id IS NULL AND r.collected_at >= ? AND r.collected_at < ?""",
        (utc_start, utc_end),
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


def get_keyword_categories(conn: sqlite3.Connection | None = None) -> dict[str, str]:
    if conn is None:
        conn = get_connection()
    rows = conn.execute(
        "SELECT keyword, category FROM keywords WHERE status = 'active'"
    ).fetchall()
    return {r["keyword"]: r["category"] for r in rows}


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
    title_ko        TEXT,
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

CREATE TABLE IF NOT EXISTS keyword_cooccurrences (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    keyword_a       TEXT NOT NULL,
    keyword_b       TEXT NOT NULL,
    mention_date    TEXT NOT NULL,
    co_count        INTEGER NOT NULL DEFAULT 0,
    sample_item_ids TEXT,
    UNIQUE(keyword_a, keyword_b, mention_date)
);
CREATE INDEX IF NOT EXISTS idx_cooccur_date ON keyword_cooccurrences(mention_date);
CREATE INDEX IF NOT EXISTS idx_cooccur_pair ON keyword_cooccurrences(keyword_a, keyword_b);

CREATE TABLE IF NOT EXISTS social_buzz (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    name            TEXT,
    mentions        INTEGER NOT NULL,
    upvotes         INTEGER NOT NULL DEFAULT 0,
    rank            INTEGER,
    rank_24h_ago    INTEGER,
    mentions_24h_ago INTEGER,
    source_filter   TEXT NOT NULL DEFAULT 'all-stocks',
    collected_date  TEXT NOT NULL,
    UNIQUE(ticker, source_filter, collected_date)
);
CREATE INDEX IF NOT EXISTS idx_buzz_ticker_date ON social_buzz(ticker, collected_date DESC);
CREATE INDEX IF NOT EXISTS idx_buzz_date ON social_buzz(collected_date DESC);

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

CREATE TABLE IF NOT EXISTS company_analyses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker          TEXT NOT NULL,
    company_name    TEXT NOT NULL,
    market          TEXT NOT NULL DEFAULT 'US',
    signal_count    INTEGER NOT NULL DEFAULT 0,
    signal_window_days INTEGER NOT NULL DEFAULT 30,
    momentum_score  INTEGER NOT NULL CHECK(momentum_score BETWEEN 0 AND 100),
    verdict         TEXT NOT NULL,
    verdict_summary TEXT NOT NULL,
    five_questions   TEXT NOT NULL,
    signal_timeline TEXT NOT NULL,
    risk_factors    TEXT NOT NULL,
    key_signals_json TEXT NOT NULL,
    model_used      TEXT NOT NULL,
    generated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    delivered       INTEGER NOT NULL DEFAULT 0,
    UNIQUE(ticker, generated_at)
);
CREATE INDEX IF NOT EXISTS idx_ca_ticker ON company_analyses(ticker);
CREATE INDEX IF NOT EXISTS idx_ca_generated ON company_analyses(generated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ca_momentum ON company_analyses(momentum_score DESC);

CREATE TABLE IF NOT EXISTS investment_theses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    thesis_date     TEXT NOT NULL,
    direction       TEXT NOT NULL,          -- 'buy' (발굴) | 'avoid' (회피/청산)
    company         TEXT NOT NULL,
    ticker          TEXT,
    market          TEXT,                   -- US | KR | JP
    bottleneck      TEXT,                   -- 핵심 병목/논거 한 줄
    reasoning       TEXT NOT NULL,          -- 2차·3차적 추론 체인
    depth_layer     INTEGER,                -- 병목 깊이 1(최심·대체불가) ~ 3(표층·진입쉬움)
    pricing_status  TEXT,                   -- 가격 반영 정도: unpriced|partial|mostly|overpriced
    conviction      TEXT,                   -- high | medium | low
    falsifier       TEXT,                   -- 이 논리가 틀렸음을 알 수 있는 조건
    driving_signals TEXT,                   -- 근거가 된 시그널 (JSON)
    model_used      TEXT NOT NULL,
    generated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    delivered       INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_theses_date ON investment_theses(thesis_date DESC);
CREATE INDEX IF NOT EXISTS idx_theses_direction ON investment_theses(direction);
"""
