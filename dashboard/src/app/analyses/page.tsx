import { getDb } from "@/lib/db";
import type { CompanyAnalysis } from "@/lib/types";
import Link from "next/link";
import { AnalysisSearch } from "./search";

export const dynamic = "force-dynamic";

function scoreColor(score: number) {
  if (score >= 70) return "bg-ember";
  if (score >= 50) return "bg-sage";
  return "bg-warm-gray";
}

function verdictColor(verdict: string) {
  if (verdict.includes("강한")) return "text-ember";
  if (verdict.includes("관심")) return "text-sage";
  if (verdict.includes("약화")) return "text-warm-gray";
  return "text-red-alert";
}

export default async function AnalysesPage() {
  const db = getDb();

  const analyses = db
    .prepare(
      `SELECT ca.*
       FROM company_analyses ca
       INNER JOIN (
         SELECT ticker, MAX(generated_at) as max_gen
         FROM company_analyses
         GROUP BY ticker
       ) latest ON ca.ticker = latest.ticker AND ca.generated_at = latest.max_gen
       ORDER BY ca.momentum_score DESC
       LIMIT 30`
    )
    .all() as CompanyAnalysis[];

  const historyCounts = db
    .prepare("SELECT ticker, COUNT(*) as cnt FROM company_analyses GROUP BY ticker")
    .all() as { ticker: string; cnt: number }[];
  const countMap = Object.fromEntries(
    historyCounts.map((r) => [r.ticker, r.cnt])
  );

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="font-serif text-2xl font-bold">기업 모멘텀 분석</h1>
        <span className="text-xs text-warm-gray">{analyses.length}개 기업</span>
      </div>

      <AnalysisSearch />

      {analyses.length === 0 ? (
        <div className="bg-white rounded-lg border border-light-gray p-8 text-center text-warm-gray">
          <p className="text-lg mb-2">아직 분석 레포트가 없습니다</p>
          <p className="text-sm">
            종목 코드를 입력하여 분석을 생성하거나, 시그널이 충분히 쌓이면
            자동으로 생성됩니다.
          </p>
        </div>
      ) : (
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4">
          {analyses.map((a) => {
            const keySignals = safeJson<{ action_note?: string }>(
              a.key_signals_json,
              {}
            );
            return (
              <Link key={a.id} href={`/analyses/${a.ticker}`} className="group">
                <div className="bg-white rounded-lg border border-light-gray p-4 h-full transition-all group-hover:shadow-md group-hover:border-sage/30">
                  <div className="flex items-start justify-between">
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-xs px-1.5 py-0.5 rounded bg-cream-dark text-warm-gray">
                          {a.market}
                        </span>
                        <span className="font-mono font-bold">{a.ticker}</span>
                      </div>
                      <p className="text-sm text-warm-gray mt-0.5 truncate">
                        {a.company_name}
                      </p>
                    </div>
                    <div
                      className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-mono font-bold text-sm shrink-0 ${scoreColor(a.momentum_score)}`}
                    >
                      {a.momentum_score}
                    </div>
                  </div>

                  <p
                    className={`font-serif font-bold mt-3 ${verdictColor(a.verdict)}`}
                  >
                    {a.verdict}
                  </p>
                  <p className="text-xs text-warm-gray line-clamp-2 mt-1">
                    {a.verdict_summary}
                  </p>

                  {keySignals.action_note && (
                    <p className="text-xs text-charcoal/70 line-clamp-2 mt-2 border-t border-light-gray pt-2">
                      {keySignals.action_note}
                    </p>
                  )}

                  <div className="flex items-center justify-between mt-3 text-xs text-warm-gray">
                    <span>{a.generated_at.slice(0, 10)}</span>
                    <div className="flex items-center gap-2">
                      <span>시그널 {a.signal_count}개</span>
                      {countMap[a.ticker] > 1 && (
                        <span className="px-1.5 py-0.5 rounded-full bg-cream-dark">
                          {countMap[a.ticker]}회
                        </span>
                      )}
                    </div>
                  </div>
                </div>
              </Link>
            );
          })}
        </div>
      )}
    </div>
  );
}

function safeJson<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}
