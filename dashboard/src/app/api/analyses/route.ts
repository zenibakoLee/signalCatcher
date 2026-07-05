import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const ticker = searchParams.get("ticker");
  const limit = Math.min(parseInt(searchParams.get("limit") || "30"), 100);

  const db = getDb();

  if (ticker) {
    const rows = db
      .prepare(
        `SELECT * FROM company_analyses WHERE ticker = ? ORDER BY generated_at DESC LIMIT ?`
      )
      .all(ticker.toUpperCase(), limit);
    return NextResponse.json({ analyses: rows });
  }

  const latest = db
    .prepare(
      `SELECT ca.*
       FROM company_analyses ca
       INNER JOIN (
         SELECT ticker, MAX(generated_at) as max_gen
         FROM company_analyses
         GROUP BY ticker
       ) latest ON ca.ticker = latest.ticker AND ca.generated_at = latest.max_gen
       ORDER BY ca.momentum_score DESC
       LIMIT ?`
    )
    .all(limit);

  return NextResponse.json({ analyses: latest });
}
