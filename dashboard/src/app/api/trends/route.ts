import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const keyword = searchParams.get("keyword");
  const days = Math.min(parseInt(searchParams.get("days") || "30"), 90);

  const db = getDb();

  if (keyword) {
    const data = db.prepare(`
      SELECT mention_date, total_count, source_breakdown
      FROM keyword_daily_aggregates
      WHERE keyword = ? AND mention_date >= date('now', ?)
      ORDER BY mention_date
    `).all(keyword, `-${days} days`);

    const alerts = db.prepare(`
      SELECT * FROM trend_alerts
      WHERE keyword = ?
      ORDER BY alert_date DESC
      LIMIT 10
    `).all(keyword);

    return NextResponse.json({ keyword, data, alerts });
  }

  const topKeywords = db.prepare(`
    SELECT keyword, SUM(total_count) as total,
           MAX(mention_date) as last_seen,
           COUNT(DISTINCT mention_date) as active_days
    FROM keyword_daily_aggregates
    WHERE mention_date >= date('now', ?)
    GROUP BY keyword
    ORDER BY total DESC
    LIMIT 20
  `).all(`-${days} days`);

  const recentAlerts = db.prepare(`
    SELECT * FROM trend_alerts
    ORDER BY alert_date DESC, z_score DESC
    LIMIT 20
  `).all();

  return NextResponse.json({ topKeywords, recentAlerts });
}
