import { getDb } from "@/lib/db";
import { NextResponse } from "next/server";

export const dynamic = "force-dynamic";

export async function GET() {
  const db = getDb();
  const keywords = db.prepare(
    "SELECT * FROM keywords ORDER BY status, category, keyword"
  ).all();
  return NextResponse.json(keywords);
}
