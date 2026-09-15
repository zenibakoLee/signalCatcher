from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

import pytest

from pipeline import db
from pipeline.models import RawItem
from pipeline.processing.dedup import deduplicate_and_store


def _raw_item(source_id: str) -> RawItem:
    return RawItem(
        source="rss",
        source_id=source_id,
        title=source_id,
        published_at=datetime(2026, 9, 15, tzinfo=UTC),
    )


def test_dedup_partial_insert_failure_rolls_back_invocation_and_preserves_preexisting(
    monkeypatch,
) -> None:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE raw_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source TEXT NOT NULL,
            source_id TEXT NOT NULL,
            title TEXT NOT NULL,
            url TEXT,
            author TEXT,
            content_snippet TEXT,
            published_at TEXT NOT NULL,
            collected_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            metadata TEXT,
            UNIQUE(source, source_id)
        );
        INSERT INTO raw_items (source, source_id, title, published_at)
        VALUES ('rss', 'preexisting', 'preexisting', '2026-09-14T00:00:00+00:00');
        CREATE TRIGGER reject_bad_raw_item
        BEFORE INSERT ON raw_items
        WHEN NEW.source_id = 'bad'
        BEGIN
            SELECT RAISE(ABORT, 'injected insert failure');
        END;
        """
    )
    monkeypatch.setattr(db, "_connection", conn)

    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        deduplicate_and_store([_raw_item("first"), _raw_item("bad")])

    rows = conn.execute(
        "SELECT source_id FROM raw_items ORDER BY id"
    ).fetchall()
    assert [row["source_id"] for row in rows] == ["preexisting"]
