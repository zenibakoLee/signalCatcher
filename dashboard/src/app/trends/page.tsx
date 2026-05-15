import { getDb } from "@/lib/db";
import { TrendChart } from "./chart";

export const dynamic = "force-dynamic";

interface KeywordSummary {
  keyword: string;
  total: number;
  last_seen: string;
  active_days: number;
}

export default function TrendsPage() {
  const db = getDb();

  const topKeywords = db.prepare(`
    SELECT keyword, SUM(total_count) as total,
           MAX(mention_date) as last_seen,
           COUNT(DISTINCT mention_date) as active_days
    FROM keyword_daily_aggregates
    WHERE mention_date >= date('now', '-30 days')
    GROUP BY keyword
    ORDER BY total DESC
    LIMIT 20
  `).all() as KeywordSummary[];

  const recentAlerts = db.prepare(`
    SELECT keyword, alert_date, z_score, severity, today_count,
           moving_avg_30d, llm_interpretation
    FROM trend_alerts
    ORDER BY alert_date DESC, z_score DESC
    LIMIT 10
  `).all() as {
    keyword: string; alert_date: string; z_score: number;
    severity: string; today_count: number; moving_avg_30d: number;
    llm_interpretation: string | null;
  }[];

  const chartData: Record<string, { date: string; count: number }[]> = {};
  for (const kw of topKeywords.slice(0, 5)) {
    const rows = db.prepare(`
      SELECT mention_date as date, total_count as count
      FROM keyword_daily_aggregates
      WHERE keyword = ? AND mention_date >= date('now', '-30 days')
      ORDER BY mention_date
    `).all(kw.keyword) as { date: string; count: number }[];
    chartData[kw.keyword] = rows;
  }

  return (
    <div className="space-y-8">
      <h1 className="font-serif text-2xl font-bold">트렌드 분석</h1>

      {recentAlerts.length > 0 && (
        <section>
          <h2 className="font-serif text-lg font-bold mb-3 text-ember">최근 트렌드 알림</h2>
          <div className="space-y-2">
            {recentAlerts.map((a, i) => (
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
        <h2 className="font-serif text-lg font-bold mb-3">상위 키워드 (30일)</h2>
        <TrendChart data={chartData} />
      </section>

      <section>
        <h2 className="font-serif text-lg font-bold mb-3">키워드 활동</h2>
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
