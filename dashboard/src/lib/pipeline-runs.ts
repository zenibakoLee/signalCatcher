import type Database from "better-sqlite3";

import { parsePipelineTimestamp } from "./freshness.ts";

const FRESH_DAILY_RUNS_SQL = `
  SELECT completed_at FROM pipeline_runs
  WHERE run_type = 'daily'
    AND status IN ('completed', 'completed_with_errors')
    AND completed_at IS NOT NULL
`;

export function getLatestFreshDailyCompletion(
  db: Database.Database,
): string | null {
  const rows = db.prepare(FRESH_DAILY_RUNS_SQL).all() as {
    completed_at: string;
  }[];
  if (rows.length === 0) return null;

  return rows.reduce((latest, candidate) => {
    const latestTime = parsePipelineTimestamp(latest.completed_at).getTime();
    const candidateTime = parsePipelineTimestamp(candidate.completed_at).getTime();
    return candidateTime > latestTime || Number.isNaN(latestTime)
      ? candidate
      : latest;
  }).completed_at;
}
