import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET(request: Request) {
  const { searchParams } = new URL(request.url);
  const source = searchParams.get("source");
  const category = searchParams.get("category");
  const minScore = parseInt(searchParams.get("minScore") || "0");
  const limit = Math.min(parseInt(searchParams.get("limit") || "50"), 200);
  const offset = parseInt(searchParams.get("offset") || "0");

  const db = getDb();

  let where = "WHERE s.score >= ?";
  const params: (string | number)[] = [minScore];

  if (source) {
    where += " AND r.source = ?";
    params.push(source);
  }
  if (category) {
    where += " AND s.category = ?";
    params.push(category);
  }

  const items = db.prepare(`
    SELECT s.score, s.score_reasoning, s.category,
           r.id, r.title, r.url, r.source, r.content_snippet, r.published_at, r.metadata
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    ${where}
    ORDER BY s.score DESC, r.published_at DESC
    LIMIT ? OFFSET ?
  `).all(...params, limit, offset);

  const total = db.prepare(`
    SELECT COUNT(*) as cnt FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    ${where}
  `).get(...params) as { cnt: number };

  return NextResponse.json({ items, total: total.cnt });
}
