import { getDb } from "@/lib/db";
import { TrendChart } from "./chart";
import { TimeRange } from "./time-range";
import { NetworkGraph } from "./network";

export const dynamic = "force-dynamic";

interface KeywordSummary {
  keyword: string;
  total: number;
  last_seen: string;
  active_days: number;
}

const VALID_DAYS = ["7", "30", "90", "all"] as const;
const ACCELERATION_WEEKS = 4;

export default async function TrendsPage({
  searchParams,
}: {
  searchParams: Promise<{ days?: string }>;
}) {
  const { days: daysParam } = await searchParams;
  const days = VALID_DAYS.includes(daysParam as typeof VALID_DAYS[number])
    ? (daysParam as string)
    : "30";

  const db = getDb();

  const dateFilter = days === "all"
    ? ""
    : `WHERE mention_date >= date('now', '-${days} days')`;

  const alertDateFilter = days === "all"
    ? ""
    : `WHERE alert_date >= date('now', '-${days} days')`;

  const topKeywords = db.prepare(`
    SELECT keyword, SUM(total_count) as total,
           MAX(mention_date) as last_seen,
           COUNT(DISTINCT mention_date) as active_days
    FROM keyword_daily_aggregates
    ${dateFilter}
    GROUP BY keyword
    ORDER BY total DESC
    LIMIT 20
  `).all() as KeywordSummary[];

  type AlertRow = {
    keyword: string; alert_date: string; z_score: number;
    severity: string; today_count: number; moving_avg_7d: number | null;
    moving_avg_30d: number; llm_interpretation: string | null;
  };

  const allAlerts = db.prepare(`
    SELECT keyword, alert_date, z_score, severity, today_count,
           moving_avg_7d, moving_avg_30d, llm_interpretation
    FROM trend_alerts
    ${alertDateFilter}
    ORDER BY alert_date DESC, z_score DESC
    LIMIT 20
  `).all() as AlertRow[];

  const spikeAlerts = allAlerts.filter((a) => a.severity !== "accelerating").slice(0, 10);
  const accelAlerts = allAlerts.filter((a) => a.severity === "accelerating").slice(0, 10);

  const chartData: Record<string, { date: string; count: number }[]> = {};
  for (const kw of topKeywords.slice(0, 5)) {
    const rows = db.prepare(`
      SELECT mention_date as date, total_count as count
      FROM keyword_daily_aggregates
      WHERE keyword = ?${days === "all" ? "" : ` AND mention_date >= date('now', '-${days} days')`}
      ORDER BY mention_date
    `).all(kw.keyword) as { date: string; count: number }[];
    chartData[kw.keyword] = rows;
  }

  const coDateFilter = days === "all"
    ? ""
    : `WHERE mention_date >= date('now', '-${days} days')`;

  let coEdges: { source: string; target: string; weight: number }[] = [];
  let coNodes: { keyword: string; totalCoCount: number }[] = [];

  try {
    const coRows = db.prepare(`
      SELECT keyword_a, keyword_b, SUM(co_count) as total
      FROM keyword_cooccurrences
      ${coDateFilter}
      GROUP BY keyword_a, keyword_b
      HAVING total >= 2
      ORDER BY total DESC
      LIMIT 30
    `).all() as { keyword_a: string; keyword_b: string; total: number }[];

    coEdges = coRows.map((r) => ({
      source: r.keyword_a,
      target: r.keyword_b,
      weight: r.total,
    }));

    const nodeSet = new Map<string, number>();
    for (const r of coRows) {
      nodeSet.set(r.keyword_a, (nodeSet.get(r.keyword_a) || 0) + r.total);
      nodeSet.set(r.keyword_b, (nodeSet.get(r.keyword_b) || 0) + r.total);
    }
    coNodes = [...nodeSet.entries()]
      .map(([keyword, totalCoCount]) => ({ keyword, totalCoCount }))
      .sort((a, b) => b.totalCoCount - a.totalCoCount)
      .slice(0, 20);
  } catch {
    // table may not exist yet
  }

  const rangeLabel = days === "all" ? "전체 기간" : `${days}일`;

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between">
        <h1 className="font-serif text-2xl font-bold">트렌드 분석</h1>
        <TimeRange current={days} />
      </div>

      {accelAlerts.length > 0 && (
        <section>
          <h2 className="font-serif text-lg font-bold mb-3 text-sage">📊 장기 가속 ({rangeLabel})</h2>
          <p className="text-sm text-warm-gray mb-3">주간 평균이 {ACCELERATION_WEEKS}주 연속 상승 중인 키워드</p>
          <div className="space-y-2">
            {accelAlerts.map((a, i) => (
              <div key={i} className="bg-white border border-sage/20 rounded-lg p-3">
                <div className="flex items-center justify-between mb-1">
                  <span className="font-bold">{a.keyword}</span>
                  <span className="px-2 py-0.5 rounded-full text-xs font-medium text-white bg-sage">
                    +{Math.round(a.z_score * 100)}% 성장
                  </span>
                </div>
                <p className="text-sm text-warm-gray">
                  최근 주간 평균 {a.moving_avg_7d?.toFixed(1)}회 · 전체 평균 {a.moving_avg_30d?.toFixed(1)}회
                </p>
                <p className="text-xs text-warm-gray mt-0.5">{a.alert_date}</p>
                {a.llm_interpretation && (
                  <p className="text-sm mt-2">{a.llm_interpretation}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {spikeAlerts.length > 0 && (
        <section>
          <h2 className="font-serif text-lg font-bold mb-3 text-ember">📈 스파이크 알림 ({rangeLabel})</h2>
          <div className="space-y-2">
            {spikeAlerts.map((a, i) => (
              <div key={i} className="bg-white border border-ember/20 rounded-lg p-3 flex items-center justify-between">
                <div>
                  <span className="font-bold">{a.keyword}</span>
                  <span className="text-sm text-warm-gray ml-2">{a.alert_date}</span>
                  {a.llm_interpretation && (
                    <p className="text-sm text-warm-gray mt-1 line-clamp-1">{a.llm_interpretation}</p>
                  )}
                </div>
                <span className={`px-2 py-0.5 rounded-full text-xs font-medium text-white ${
                  a.severity === "urgent" ? "bg-red-alert" : "bg-ember"
                }`}>
                  z={a.z_score}
                </span>
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="font-serif text-lg font-bold mb-3">상위 키워드 ({rangeLabel})</h2>
        <TrendChart data={chartData} />
      </section>

      {coNodes.length > 0 && (
        <section>
          <h2 className="font-serif text-lg font-bold mb-3 text-deep-blue">🔗 키워드 상관관계 ({rangeLabel})</h2>
          <p className="text-sm text-warm-gray mb-3">같은 아이템에서 함께 출현하는 키워드 쌍</p>
          <NetworkGraph nodes={coNodes} edges={coEdges} />
        </section>
      )}

      <section>
        <h2 className="font-serif text-lg font-bold mb-3">키워드 활동 ({rangeLabel})</h2>
        <div className="bg-white rounded-lg border border-light-gray overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-light-gray bg-cream-dark/50">
                <th className="text-left p-3 font-medium">키워드</th>
                <th className="text-right p-3 font-medium">총 언급</th>
                <th className="text-right p-3 font-medium">활동일</th>
                <th className="text-right p-3 font-medium">최근</th>
              </tr>
            </thead>
            <tbody>
              {topKeywords.map((kw) => (
                <tr key={kw.keyword} className="border-b border-light-gray/50 hover:bg-cream-dark/30">
                  <td className="p-3 font-medium">{kw.keyword}</td>
                  <td className="p-3 text-right font-mono">{kw.total}</td>
                  <td className="p-3 text-right">{kw.active_days}일</td>
                  <td className="p-3 text-right text-warm-gray">{kw.last_seen}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
