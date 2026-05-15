import { NextResponse } from "next/server";
import { statSync } from "fs";
import { join } from "path";

export const dynamic = "force-dynamic";

const DB_PATH = join(process.cwd(), "..", "data", "signalcatcher.db");

export function GET() {
  try {
    const stat = statSync(DB_PATH);
    return NextResponse.json({ mtime: stat.mtimeMs });
  } catch {
    return NextResponse.json({ mtime: 0 });
  }
}
