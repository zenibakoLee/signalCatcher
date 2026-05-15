import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const date = searchParams.get("date");

  const db = getDb();

  if (date) {
    const digest = db.prepare("SELECT * FROM digests WHERE digest_date = ?").get(date);
    return NextResponse.json(digest || null);
  }

  const latest = db.prepare("SELECT * FROM digests ORDER BY digest_date DESC LIMIT 1").get();
  if (!latest) return NextResponse.json(null);

  const digestDate = (latest as { digest_date: string }).digest_date;

  const alerts = db.prepare(
    "SELECT * FROM trend_alerts WHERE alert_date = ? ORDER BY z_score DESC"
  ).all(digestDate);

  const topItems = db.prepare(`
    SELECT s.score, s.score_reasoning, s.category,
           r.title, r.url, r.source, r.content_snippet, r.published_at
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    WHERE date(r.collected_at) = ?
    ORDER BY s.score DESC
    LIMIT 15
  `).all(digestDate);

  return NextResponse.json({ digest: latest, alerts, topItems });
}
