from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
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
    if conn.in_transaction:
        raise RuntimeError("database initialization requires no open transaction")
    try:
        conn.executescript("BEGIN IMMEDIATE;\n" + _SCHEMA)
        _migrate_scored_items(conn)
        _migrate_theses(conn)
        _migrate_security_master(conn)
        _migrate_weekly_superstar(conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise


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


def _migrate_security_master(conn: sqlite3.Connection) -> None:
    """Add SEC security-master storage atomically without rewriting records."""
    conn.execute("SAVEPOINT security_master_migration")
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS security_master (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticker TEXT NOT NULL,
                company_name TEXT NOT NULL,
                cik INTEGER NOT NULL,
                exchange TEXT,
                listing_status TEXT,
                verification_source_url TEXT,
                verification_as_of TEXT,
                source_url TEXT NOT NULL,
                source_as_of TEXT,
                fetched_at TEXT NOT NULL,
                source_status TEXT NOT NULL,
                UNIQUE(ticker, source_url, fetched_at)
            )
        """)
        security_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(security_master)").fetchall()
        }
        if "verification_source_url" not in security_columns:
            conn.execute(
                "ALTER TABLE security_master ADD COLUMN verification_source_url TEXT"
            )
        if "verification_as_of" not in security_columns:
            conn.execute(
                "ALTER TABLE security_master ADD COLUMN verification_as_of TEXT"
            )
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(candidate_snapshots)").fetchall()
        }
        if columns and "security_master_id" not in columns:
            conn.execute(
                "ALTER TABLE candidate_snapshots "
                "ADD COLUMN security_master_id INTEGER REFERENCES security_master(id)"
            )
            columns.add("security_master_id")
        if {"status", "coverage", "security_master_id"} <= columns:
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS candidate_verified_listing_fk_insert
                BEFORE INSERT ON candidate_snapshots
                WHEN NEW.security_master_id IS NULL AND (
                    NEW.status = 'scored' OR EXISTS (
                        SELECT 1 FROM json_each(NEW.coverage, '$.present')
                        WHERE value IN (
                            'verified_us_listing',
                            'verified_operating_issuer_sole_exchange_ticker_periodic'
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'verified listing requires security_master_id');
                END
            """)
            conn.execute("""
                CREATE TRIGGER IF NOT EXISTS candidate_verified_listing_fk_update
                BEFORE UPDATE ON candidate_snapshots
                WHEN NEW.security_master_id IS NULL AND (
                    NEW.status = 'scored' OR EXISTS (
                        SELECT 1 FROM json_each(NEW.coverage, '$.present')
                        WHERE value IN (
                            'verified_us_listing',
                            'verified_operating_issuer_sole_exchange_ticker_periodic'
                        )
                    )
                )
                BEGIN
                    SELECT RAISE(ABORT, 'verified listing requires security_master_id');
                END
            """)
    except Exception:
        conn.execute("ROLLBACK TO security_master_migration")
        conn.execute("RELEASE security_master_migration")
        raise
    else:
        conn.execute("RELEASE security_master_migration")


def _migrate_weekly_superstar(conn: sqlite3.Connection) -> None:
    """Install the six-stage weekly audit and preserve legacy provenance."""
    conn.execute("SAVEPOINT weekly_superstar_migration")
    try:
        run_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(pipeline_runs)").fetchall()
        }
        for name in ("input_items", "candidates_considered", "candidates_published"):
            if name not in run_columns:
                conn.execute(
                    f"ALTER TABLE pipeline_runs ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                )
        snapshot_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(weekly_superstar_snapshots)"
            ).fetchall()
        }
        if snapshot_columns:
            if "coverage_ratio" not in snapshot_columns:
                conn.execute(
                    "ALTER TABLE weekly_superstar_snapshots "
                    "ADD COLUMN coverage_ratio REAL NOT NULL DEFAULT 0.0 "
                    "CHECK(coverage_ratio >= 0.0 AND coverage_ratio <= 1.0)"
                )
            if "missing_evidence" not in snapshot_columns:
                conn.execute(
                    "ALTER TABLE weekly_superstar_snapshots "
                    "ADD COLUMN missing_evidence TEXT NOT NULL "
                    "DEFAULT '[\"authoritative_stage_measurements\"]'"
                )

        stage_sql_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='weekly_superstar_stage_evidence'"
        ).fetchone()
        if stage_sql_row and (
            "switching_costs_standard" in stage_sql_row[0]
            or "switching_costs_rise" not in stage_sql_row[0]
            or "de_facto_standard" not in stage_sql_row[0]
        ):
            conn.execute("DROP INDEX IF EXISTS idx_weekly_stage_raw_once")
            conn.execute(
                "ALTER TABLE weekly_superstar_stage_evidence "
                "RENAME TO weekly_superstar_stage_evidence_legacy_v0"
            )
        for statement in _WEEKLY_SUPERSTAR_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(statement)
        for statement in _WEEKLY_SUPERSTAR_FILTER_V2_SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(statement)

        legacy_exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='weekly_superstar_stage_evidence_legacy_v0'"
        ).fetchone()
        if legacy_exists:
            conn.execute(
                """INSERT INTO weekly_superstar_stage_evidence
                       (snapshot_id, stage, status, claim, raw_item_id, source, source_date)
                   SELECT legacy.snapshot_id,
                          CASE legacy.stage
                              WHEN 'switching_costs_standard' THEN 'switching_costs_rise'
                              ELSE legacy.stage
                          END,
                          legacy.status, legacy.claim, legacy.raw_item_id,
                          legacy.source, legacy.source_date
                   FROM weekly_superstar_stage_evidence_legacy_v0 AS legacy
                   WHERE legacy.stage IN (
                       'wedge_product', 'adjacent_products_attach',
                       'customer_data_workflow_accumulates', 'third_party_developers_join',
                       'switching_costs_standard', 'switching_costs_rise',
                       'de_facto_standard'
                   )
                     AND EXISTS (
                         SELECT 1 FROM weekly_superstar_snapshots AS snapshot
                         WHERE snapshot.id = legacy.snapshot_id
                     )
                     AND (
                         (
                             legacy.status = 'proven'
                             AND legacy.raw_item_id IS NOT NULL
                             AND legacy.source IS NOT NULL
                             AND legacy.source_date IS NOT NULL
                             AND EXISTS (
                                 SELECT 1 FROM raw_items
                                 WHERE id = legacy.raw_item_id
                             )
                             AND legacy.rowid = (
                                 SELECT MIN(duplicate.rowid)
                                 FROM weekly_superstar_stage_evidence_legacy_v0 AS duplicate
                                 WHERE duplicate.snapshot_id = legacy.snapshot_id
                                   AND duplicate.raw_item_id = legacy.raw_item_id
                             )
                         )
                         OR (
                             legacy.status = 'missing'
                             AND legacy.raw_item_id IS NULL
                             AND legacy.source IS NULL
                             AND legacy.source_date IS NULL
                             AND legacy.rowid = (
                                 SELECT MIN(duplicate.rowid)
                                 FROM weekly_superstar_stage_evidence_legacy_v0 AS duplicate
                                 WHERE duplicate.snapshot_id = legacy.snapshot_id
                                   AND CASE duplicate.stage
                                       WHEN 'switching_costs_standard'
                                           THEN 'switching_costs_rise'
                                       ELSE duplicate.stage
                                   END = CASE legacy.stage
                                       WHEN 'switching_costs_standard'
                                           THEN 'switching_costs_rise'
                                       ELSE legacy.stage
                                   END
                                   AND duplicate.status = 'missing'
                                   AND duplicate.raw_item_id IS NULL
                                   AND duplicate.source IS NULL
                                   AND duplicate.source_date IS NULL
                             )
                         )
                     )
                     AND NOT EXISTS (
                         SELECT 1 FROM weekly_superstar_stage_evidence AS active
                         WHERE active.snapshot_id = legacy.snapshot_id
                           AND (
                               (
                                   legacy.raw_item_id IS NOT NULL
                                   AND active.raw_item_id = legacy.raw_item_id
                               )
                               OR (
                                   legacy.raw_item_id IS NULL
                                   AND active.stage = CASE legacy.stage
                                       WHEN 'switching_costs_standard'
                                           THEN 'switching_costs_rise'
                                       ELSE legacy.stage
                                   END
                                   AND active.status = 'missing'
                               )
                           )
                     )"""
            )
        conn.execute(
            """INSERT INTO weekly_superstar_stage_evidence
                   (snapshot_id, stage, status, claim)
               SELECT snapshot.id, 'de_facto_standard', 'missing', ''
               FROM weekly_superstar_snapshots AS snapshot
               WHERE snapshot.feature_version = 'weekly-superstar-platform-v1'
                 AND NOT EXISTS (
                   SELECT 1 FROM weekly_superstar_stage_evidence AS evidence
                   WHERE evidence.snapshot_id = snapshot.id
                     AND evidence.stage = 'de_facto_standard'
               )"""
        )
        conn.execute(
            """UPDATE weekly_superstar_snapshots
               SET status = CASE
                       WHEN status = 'unverified_us_listing' THEN status
                       ELSE 'insufficient_evidence'
                   END,
                   score = NULL,
                   rank = NULL,
                   coverage_ratio = MIN(1.0, MAX(0.0, (
                       SELECT COUNT(DISTINCT evidence.stage) / 6.0
                       FROM weekly_superstar_stage_evidence AS evidence
                       WHERE evidence.snapshot_id = weekly_superstar_snapshots.id
                         AND evidence.status = 'proven'
                   ))),
                   missing_evidence = CASE
                       WHEN json_valid(missing_evidence)
                            AND json_type(missing_evidence) = 'array'
                            AND EXISTS (
                                SELECT 1 FROM json_each(missing_evidence)
                                WHERE value = 'authoritative_stage_measurements'
                            )
                       THEN missing_evidence
                       WHEN json_valid(missing_evidence)
                            AND json_type(missing_evidence) = 'array'
                       THEN json_insert(
                           missing_evidence, '$[#]',
                           'authoritative_stage_measurements'
                       )
                       ELSE '[\"authoritative_stage_measurements\"]'
                   END
               WHERE feature_version = 'weekly-superstar-platform-v1'"""
        )
        conn.execute(
            """UPDATE pipeline_runs
               SET candidates_published = 0
               WHERE id IN (
                   SELECT pipeline_run_id FROM weekly_superstar_snapshots
                   WHERE feature_version = 'weekly-superstar-platform-v1'
               )"""
        )
        conn.execute("DROP TRIGGER IF EXISTS weekly_superstar_v1_no_qualified_insert")
        conn.execute("DROP TRIGGER IF EXISTS weekly_superstar_v1_no_qualified_update")
        conn.execute("DROP TRIGGER IF EXISTS weekly_superstar_v1_no_published_run")
        for statement in _WEEKLY_SUPERSTAR_TRIGGERS:
            conn.execute(statement)
    except Exception:
        conn.execute("ROLLBACK TO weekly_superstar_migration")
        conn.execute("RELEASE weekly_superstar_migration")
        raise
    else:
        conn.execute("RELEASE weekly_superstar_migration")


def _execute_raw_item_insert(
    conn: sqlite3.Connection, item: RawItem
) -> sqlite3.Cursor:
    published_at = item.published_at
    if published_at.tzinfo is not None and published_at.utcoffset() is not None:
        published_at = published_at.astimezone(UTC)
    return conn.execute(
        """INSERT OR IGNORE INTO raw_items
           (source, source_id, title, url, author, content_snippet,
            published_at, collected_at, metadata)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.source,
            item.source_id,
            item.title,
            item.url,
            item.author,
            item.content_snippet,
            published_at.isoformat(),
            datetime.now(UTC).isoformat(),
            json.dumps(item.metadata) if item.metadata else None,
        ),
    )


def insert_raw_item(item: RawItem) -> int | None:
    conn = get_connection()
    try:
        cur = _execute_raw_item_insert(conn, item)
        conn.commit()
        if cur.rowcount == 0:
            return None
        return cur.lastrowid
    except sqlite3.Error:
        conn.rollback()
        raise


def insert_raw_items(items: list[RawItem]) -> list[int]:
    """Insert one deduplication batch atomically and return committed row IDs."""
    conn = get_connection()
    if conn.in_transaction:
        raise RuntimeError("raw item batch insert requires no open transaction")
    new_ids: list[int] = []
    try:
        conn.execute("BEGIN IMMEDIATE")
        for item in items:
            cur = _execute_raw_item_insert(conn, item)
            if cur.rowcount == 1:
                if cur.lastrowid is None:
                    raise RuntimeError("raw item insert returned no row ID")
                new_ids.append(cur.lastrowid)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return new_ids


def _kst_date_to_utc_range(date_str: str) -> tuple[str, str]:
    """Convert a KST date string (YYYY-MM-DD) to UTC start/end timestamps."""
    KST = timezone(timedelta(hours=9))
    parts = [int(p) for p in date_str.split("-")]
    kst_start = datetime(parts[0], parts[1], parts[2], tzinfo=KST)
    utc_start = kst_start.astimezone(UTC)
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

    cur = conn.execute(
        "INSERT INTO pipeline_runs (run_type, started_at) VALUES (?, ?)",
        (run_type, datetime.now(UTC).isoformat()),
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
    input_items: int = 0,
    candidates_considered: int = 0,
    candidates_published: int = 0,
) -> None:
    conn = get_connection()

    conn.execute(
        """UPDATE pipeline_runs
           SET completed_at=?, status=?, items_collected=?, items_scored=?, errors=?, duration_secs=?,
               input_items=?, candidates_considered=?, candidates_published=?
           WHERE id=?""",
        (
            datetime.now(UTC).isoformat(),
            status,
            items_collected,
            items_scored,
            json.dumps(errors) if errors else None,
            duration_secs,
            input_items,
            candidates_considered,
            candidates_published,
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
    collected_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
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
    duration_secs   REAL,
    input_items INTEGER NOT NULL DEFAULT 0,
    candidates_considered INTEGER NOT NULL DEFAULT 0,
    candidates_published INTEGER NOT NULL DEFAULT 0
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

-- Preliminary v1 discovery output. Trend keywords are evidence-backed emerging
-- signals until a future grouping system supplies strict theme membership proof.
CREATE TABLE IF NOT EXISTS security_master (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company_name TEXT NOT NULL,
    cik INTEGER NOT NULL,
    exchange TEXT,
    listing_status TEXT,
    verification_source_url TEXT,
    verification_as_of TEXT,
    source_url TEXT NOT NULL,
    source_as_of TEXT,
    fetched_at TEXT NOT NULL,
    source_status TEXT NOT NULL,
    UNIQUE(ticker, source_url, fetched_at)
);

CREATE TABLE IF NOT EXISTS themes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_key TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    signal_kind TEXT NOT NULL CHECK(signal_kind IN ('emerging_signal', 'strict_theme')),
    strict_grouping_evidence INTEGER NOT NULL DEFAULT 0 CHECK(strict_grouping_evidence IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now'))
);

CREATE TABLE IF NOT EXISTS theme_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theme_id INTEGER NOT NULL REFERENCES themes(id),
    as_of TEXT NOT NULL,
    pipeline_run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
    feature_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scored', 'insufficient_evidence')),
    score REAL CHECK(score IS NULL OR score BETWEEN 0 AND 100),
    rank INTEGER,
    coverage TEXT NOT NULL,
    feature_values TEXT NOT NULL,
    evidence_raw_item_ids TEXT NOT NULL,
    evidence_sources TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(theme_id, as_of, pipeline_run_id, feature_version)
);
CREATE INDEX IF NOT EXISTS idx_theme_snapshots_as_of ON theme_snapshots(as_of DESC);

CREATE TABLE IF NOT EXISTS candidate_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    market TEXT NOT NULL,
    hypothesis_category TEXT CHECK(hypothesis_category IN ('buy', 'avoid')),
    thesis_id INTEGER REFERENCES investment_theses(id),
    company_analysis_id INTEGER REFERENCES company_analyses(id),
    security_master_id INTEGER REFERENCES security_master(id),
    as_of TEXT NOT NULL,
    pipeline_run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
    feature_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('scored', 'insufficient_evidence', 'unverified_us_listing', 'ineligible_market')),
    score REAL CHECK(score IS NULL OR score BETWEEN 0 AND 100),
    rank INTEGER,
    coverage TEXT NOT NULL,
    feature_values TEXT NOT NULL,
    evidence_raw_item_ids TEXT NOT NULL,
    evidence_sources TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S','now')),
    UNIQUE(ticker, as_of, pipeline_run_id, feature_version)
);
CREATE INDEX IF NOT EXISTS idx_candidate_snapshots_as_of_rank ON candidate_snapshots(as_of DESC, rank ASC);
CREATE TRIGGER IF NOT EXISTS candidate_verified_listing_fk_insert
BEFORE INSERT ON candidate_snapshots
WHEN NEW.security_master_id IS NULL AND (
    NEW.status = 'scored' OR EXISTS (
        SELECT 1 FROM json_each(NEW.coverage, '$.present')
        WHERE value IN (
            'verified_us_listing',
            'verified_operating_issuer_sole_exchange_ticker_periodic'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'verified listing requires security_master_id');
END;
CREATE TRIGGER IF NOT EXISTS candidate_verified_listing_fk_update
BEFORE UPDATE ON candidate_snapshots
WHEN NEW.security_master_id IS NULL AND (
    NEW.status = 'scored' OR EXISTS (
        SELECT 1 FROM json_each(NEW.coverage, '$.present')
        WHERE value IN (
            'verified_us_listing',
            'verified_operating_issuer_sole_exchange_ticker_periodic'
        )
    )
)
BEGIN
    SELECT RAISE(ABORT, 'verified listing requires security_master_id');
END;
"""


_WEEKLY_SUPERSTAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_superstar_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker TEXT NOT NULL,
    company TEXT NOT NULL,
    as_of TEXT NOT NULL,
    pipeline_run_id INTEGER NOT NULL REFERENCES pipeline_runs(id),
    feature_version TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'insufficient_evidence', 'unverified_us_listing'
    )),
    security_master_id INTEGER REFERENCES security_master(id),
    missing_stages TEXT NOT NULL,
    source_count INTEGER NOT NULL,
    coverage_ratio REAL NOT NULL CHECK(coverage_ratio >= 0.0 AND coverage_ratio <= 1.0),
    missing_evidence TEXT NOT NULL,
    score REAL,
    rank INTEGER,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
    UNIQUE(ticker, as_of, pipeline_run_id, feature_version),
    CHECK(score IS NULL),
    CHECK(rank IS NULL)
);
CREATE INDEX IF NOT EXISTS idx_weekly_superstar_as_of
ON weekly_superstar_snapshots(as_of DESC, status, ticker);

CREATE TABLE IF NOT EXISTS weekly_superstar_stage_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES weekly_superstar_snapshots(id) ON DELETE CASCADE,
    stage TEXT NOT NULL CHECK(stage IN (
        'wedge_product', 'adjacent_products_attach', 'customer_data_workflow_accumulates',
        'third_party_developers_join', 'switching_costs_rise', 'de_facto_standard'
    )),
    status TEXT NOT NULL CHECK(status IN ('proven', 'missing')),
    claim TEXT NOT NULL,
    raw_item_id INTEGER REFERENCES raw_items(id),
    source TEXT,
    source_date TEXT,
    CHECK(
        (status = 'proven' AND raw_item_id IS NOT NULL AND source IS NOT NULL AND source_date IS NOT NULL)
        OR (status = 'missing' AND raw_item_id IS NULL AND source IS NULL AND source_date IS NULL)
    ),
    UNIQUE(snapshot_id, stage, raw_item_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_weekly_stage_raw_once
ON weekly_superstar_stage_evidence(snapshot_id, raw_item_id)
WHERE raw_item_id IS NOT NULL;
"""


_WEEKLY_SUPERSTAR_FILTER_V2_SCHEMA = """
CREATE TABLE IF NOT EXISTS weekly_superstar_filter_audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    weekly_snapshot_id INTEGER NOT NULL UNIQUE
        REFERENCES weekly_superstar_snapshots(id) ON DELETE CASCADE,
    filter_version TEXT NOT NULL,
    document_source_url TEXT NOT NULL,
    document_source_version TEXT NOT NULL,
    industry_track TEXT NOT NULL CHECK(industry_track IN (
        'software_platform', 'semiconductor_industrial',
        'energy_infrastructure', 'other_unclassified'
    )),
    classification TEXT CHECK(classification IN (
        'proven_compounder', 'confirmed_inflection', 'pre_inflection',
        'mature_fully_priced', 'false_positive'
    )),
    classification_status TEXT NOT NULL CHECK(classification_status IN (
        'unclassified', 'classified'
    )),
    authoritative_measurements_present INTEGER NOT NULL DEFAULT 0
        CHECK(authoritative_measurements_present IN (0, 1)),
    display_ready INTEGER NOT NULL DEFAULT 0 CHECK(display_ready IN (0, 1)),
    missing_evidence TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%S+00:00','now')),
    CHECK(
        authoritative_measurements_present = 1
        OR (classification IS NULL AND classification_status = 'unclassified')
    )
);
CREATE INDEX IF NOT EXISTS idx_weekly_superstar_filter_version
ON weekly_superstar_filter_audits(filter_version, display_ready, id);

CREATE TABLE IF NOT EXISTS weekly_superstar_filter_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_id INTEGER NOT NULL REFERENCES weekly_superstar_filter_audits(id) ON DELETE CASCADE,
    criterion_scope TEXT NOT NULL CHECK(criterion_scope IN ('core', 'industry')),
    criterion_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
    status TEXT NOT NULL CHECK(status IN ('supported', 'partial', 'missing')),
    claim TEXT NOT NULL,
    missing_evidence TEXT NOT NULL,
    UNIQUE(audit_id, criterion_key),
    UNIQUE(audit_id, ordinal),
    CHECK(status != 'missing' OR claim = '')
);

CREATE TABLE IF NOT EXISTS weekly_superstar_filter_citations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    criterion_id INTEGER NOT NULL
        REFERENCES weekly_superstar_filter_criteria(id) ON DELETE CASCADE,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items(id),
    source TEXT NOT NULL,
    source_date TEXT NOT NULL,
    UNIQUE(criterion_id, raw_item_id)
);
"""


_WEEKLY_SUPERSTAR_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS weekly_superstar_v1_no_qualified_insert
       BEFORE INSERT ON weekly_superstar_snapshots
       WHEN NEW.feature_version = 'weekly-superstar-platform-v1'
            AND (
                NEW.status NOT IN ('insufficient_evidence', 'unverified_us_listing')
                OR NEW.score IS NOT NULL
                OR NEW.rank IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM pipeline_runs
                    WHERE id = NEW.pipeline_run_id AND candidates_published != 0
                )
            )
       BEGIN
           SELECT RAISE(ABORT, 'weekly Superstar v1 NO-GO violation');
       END""",
    """CREATE TRIGGER IF NOT EXISTS weekly_superstar_v1_no_qualified_update
       BEFORE UPDATE ON weekly_superstar_snapshots
       WHEN NEW.feature_version = 'weekly-superstar-platform-v1'
            AND (
                NEW.status NOT IN ('insufficient_evidence', 'unverified_us_listing')
                OR NEW.score IS NOT NULL
                OR NEW.rank IS NOT NULL
                OR EXISTS (
                    SELECT 1 FROM pipeline_runs
                    WHERE id = NEW.pipeline_run_id AND candidates_published != 0
                )
            )
       BEGIN
           SELECT RAISE(ABORT, 'weekly Superstar v1 NO-GO violation');
       END""",
    """CREATE TRIGGER IF NOT EXISTS weekly_superstar_v1_no_published_run
       BEFORE UPDATE OF candidates_published ON pipeline_runs
       WHEN NEW.candidates_published != 0
            AND EXISTS (
                SELECT 1 FROM weekly_superstar_snapshots
                WHERE pipeline_run_id = NEW.id
                  AND feature_version = 'weekly-superstar-platform-v1'
            )
       BEGIN
           SELECT RAISE(ABORT, 'weekly Superstar v1 NO-GO violation');
       END""",
)
