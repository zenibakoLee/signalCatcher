import { getDb } from "@/lib/db";
import { execFile } from "child_process";
import { NextResponse } from "next/server";
import path from "path";

export const dynamic = "force-dynamic";

export async function POST(request: Request) {
  const { ticker } = await request.json();

  if (!ticker || !/^[A-Za-z0-9가-힣]{1,10}$/.test(ticker)) {
    return NextResponse.json({ error: "invalid_ticker" }, { status: 400 });
  }

  const upperTicker = ticker.toUpperCase();
  const projectRoot = path.resolve(process.cwd(), "..");
  const pythonPath = path.join(projectRoot, ".venv", "bin", "python");
  const scriptPath = path.join(
    projectRoot,
    "pipeline",
    "scripts",
    "generate_analysis.py"
  );

  try {
    const result = await new Promise<string>((resolve, reject) => {
      execFile(
        pythonPath,
        [scriptPath, upperTicker],
        { cwd: projectRoot, timeout: 120_000 },
        (error, stdout, stderr) => {
          if (error) reject(new Error(stderr || error.message));
          else resolve(stdout);
        }
      );
    });

    const data = JSON.parse(result);
    if (data.error) {
      return NextResponse.json(data, { status: 500 });
    }

    const db = getDb();
    const analysis = db
      .prepare(
        "SELECT * FROM company_analyses WHERE ticker = ? ORDER BY generated_at DESC LIMIT 1"
      )
      .get(data.ticker);

    return NextResponse.json({ success: true, analysis });
  } catch (e: unknown) {
    const message = e instanceof Error ? e.message : String(e);
    return NextResponse.json(
      { error: "generation_failed", message },
      { status: 500 }
    );
  }
}
