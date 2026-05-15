import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const keyword = searchParams.get("keyword");
  const daysRaw = searchParams.get("days") || "30";
  const isAll = daysRaw === "all";
  const days = isAll ? 0 : Math.min(parseInt(daysRaw) || 30, 365);

  const db = getDb();

  const dateClause = isAll ? "" : `AND mention_date >= date('now', '-${days} days')`;

  if (keyword) {
    const data = db.prepare(`
      SELECT mention_date, total_count, source_breakdown
      FROM keyword_daily_aggregates
      WHERE keyword = ? ${dateClause}
      ORDER BY mention_date
    `).all(keyword);

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
    ${isAll ? "" : `WHERE mention_date >= date('now', '-${days} days')`}
    GROUP BY keyword
    ORDER BY total DESC
    LIMIT 20
  `).all();

  const recentAlerts = db.prepare(`
    SELECT * FROM trend_alerts
    ORDER BY alert_date DESC, z_score DESC
    LIMIT 20
  `).all();

  return NextResponse.json({ topKeywords, recentAlerts });
}
