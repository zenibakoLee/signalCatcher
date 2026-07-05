import { getDb } from "@/lib/db";
import type { CompanyAnalysis } from "@/lib/types";
import Link from "next/link";
import { notFound } from "next/navigation";
import { AnalysisDetail } from "./detail";
import { RegenerateButton } from "./regenerate";

export const dynamic = "force-dynamic";

export default async function TickerAnalysisPage({
  params,
}: {
  params: Promise<{ ticker: string }>;
}) {
  const { ticker } = await params;
  const db = getDb();

  const analyses = db
    .prepare(
      "SELECT * FROM company_analyses WHERE ticker = ? ORDER BY generated_at DESC LIMIT 20"
    )
    .all(ticker.toUpperCase()) as CompanyAnalysis[];

  if (analyses.length === 0) {
    notFound();
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-3">
          <Link
            href="/analyses"
            className="text-sm text-warm-gray hover:text-sage transition-colors"
          >
            ← 전체 목록
          </Link>
          <span className="text-light-gray">/</span>
          <div className="flex items-center gap-2">
            <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-cream-dark text-warm-gray">
              {analyses[0].market}
            </span>
            <h1 className="font-serif text-2xl font-bold">
              {ticker.toUpperCase()}
            </h1>
            <span className="text-sm text-warm-gray">
              {analyses[0].company_name}
            </span>
          </div>
        </div>
        <RegenerateButton ticker={ticker.toUpperCase()} />
      </div>

      <AnalysisDetail analyses={JSON.parse(JSON.stringify(analyses))} />
    </div>
  );
}
