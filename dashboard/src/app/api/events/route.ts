import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();

  const briefings = db.prepare(`
    SELECT id, conference_name, conference_start, conference_end,
           briefing_type, content_md, expected_items, silent_signals, generated_at
    FROM conference_briefings
    ORDER BY conference_start DESC, briefing_type DESC
  `).all();

  return NextResponse.json(briefings);
}
