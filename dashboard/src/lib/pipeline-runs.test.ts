import assert from "node:assert/strict";
import test from "node:test";

import Database from "better-sqlite3";

import { getLatestFreshDailyCompletion } from "./pipeline-runs.ts";


test("completed_with_errors counts as the latest fresh daily run", () => {
  const db = new Database(":memory:");
  db.exec(`
    CREATE TABLE pipeline_runs (
      run_type TEXT NOT NULL,
      status TEXT NOT NULL,
      completed_at TEXT
    )
  `);
  const insert = db.prepare(
    "INSERT INTO pipeline_runs (run_type, status, completed_at) VALUES (?, ?, ?)",
  );
  insert.run("daily", "completed", "2026-09-13T00:00:00+00:00");
  insert.run("daily", "completed_with_errors", "2026-09-13T01:00:00+00:00");
  insert.run("daily", "failed", "2026-09-13T02:00:00+00:00");
  insert.run("event_post", "completed", "2026-09-13T03:00:00+00:00");

  assert.equal(
    getLatestFreshDailyCompletion(db),
    "2026-09-13T01:00:00+00:00",
  );

  db.close();
});

test("orders mixed legacy KST and offset-aware timestamps by instant", () => {
  const db = new Database(":memory:");
  db.exec(`
    CREATE TABLE pipeline_runs (
      run_type TEXT NOT NULL,
      status TEXT NOT NULL,
      completed_at TEXT
    )
  `);
  const insert = db.prepare(
    "INSERT INTO pipeline_runs (run_type, status, completed_at) VALUES (?, ?, ?)",
  );
  insert.run("daily", "completed", "2026-09-13T07:00:00");
  insert.run("daily", "completed", "2026-09-13T00:00:00+00:00");

  assert.equal(
    getLatestFreshDailyCompletion(db),
    "2026-09-13T00:00:00+00:00",
  );

  db.close();
});
