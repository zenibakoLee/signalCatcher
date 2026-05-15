import { getDb } from "@/lib/db";
import type { ScoredItem, TrendAlert, Digest } from "@/lib/types";
import { DatePicker } from "@/components/date-picker";

export const dynamic = "force-dynamic";

function scoreEmoji(score: number) {
  if (score >= 90) return "🔴";
  if (score >= 70) return "🟡";
  return "🟢";
}

function sourceLabel(source: string) {
  const labels: Record<string, string> = {
    hackernews: "HN", arxiv: "arXiv", github: "GitHub", rss: "RSS", youtube: "YT",
  };
  return labels[source] || source;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ date?: string }>;
}) {
  const { date } = await searchParams;
  const db = getDb();

  const availableDates = (
    db.prepare("SELECT digest_date FROM digests ORDER BY digest_date ASC").all() as { digest_date: string }[]
  ).map((r) => r.digest_date);

  if (availableDates.length === 0) {
    return (
      <div className="text-center py-20">
        <h1 className="font-serif text-3xl font-bold mb-4">시그널 캐처</h1>
        <p className="text-warm-gray">아직 생성된 다이제스트가 없습니다.</p>
      </div>
    );
  }

  const targetDate = date && availableDates.includes(date)
    ? date
    : availableDates[availableDates.length - 1];

  const digest = db.prepare(
    "SELECT * FROM digests WHERE digest_date = ?"
  ).get(targetDate) as Digest;

  const topItems = db.prepare(`
    SELECT s.score, s.score_reasoning, s.category, s.title_ko,
           r.title, r.url, r.source, r.content_snippet
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    WHERE date(r.collected_at) = ?
    ORDER BY s.score DESC LIMIT 10
  `).all(targetDate) as ScoredItem[];

  const alerts = db.prepare(
    "SELECT * FROM trend_alerts WHERE alert_date = ? ORDER BY z_score DESC"
  ).all(targetDate) as TrendAlert[];

  return (
    <div className="space-y-8">
      <header className="border-b border-light-gray pb-6">
        <div className="relative mb-3">
          <DatePicker currentDate={targetDate} availableDates={availableDates} />
        </div>
        <h1 className="font-serif text-3xl font-bold leading-tight mb-3">{digest.headline}</h1>
      </header>

      {alerts.length > 0 && (
        <section>
          <h2 className="font-serif text-xl font-bold mb-4 text-ember">📈 트렌드 알림</h2>
          <div className="grid gap-3">
            {alerts.map((a) => (
              <div key={a.keyword} className="bg-white border border-ember/20 rounded-lg p-4">
                <div className="flex items-center justify-between mb-2">
                  <span className="font-bold">{a.keyword}</span>
                  <span className={`px-2 py-0.5 rounded-full text-xs font-medium text-white ${
                    a.severity === "urgent" ? "bg-red-alert" : "bg-ember"
                  }`}>
                    {a.severity === "urgent" ? "긴급" : "주목"} · z={a.z_score}
                  </span>
                </div>
                <p className="text-sm text-warm-gray">
                  오늘 {a.today_count}회 vs 30일 평균 {a.moving_avg_30d?.toFixed(1)}회
                </p>
                {a.llm_interpretation && (
                  <p className="text-sm mt-2">{a.llm_interpretation}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      <section>
        <h2 className="font-serif text-xl font-bold mb-4">시그널 Top 10</h2>
        <div className="space-y-3">
          {topItems.map((item, i) => (
            <div key={i} className="bg-white rounded-lg border border-light-gray p-4 hover:border-sage/40 transition-colors">
              <div className="flex items-start gap-3">
                <span className="text-lg mt-0.5">{scoreEmoji(item.score)}</span>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs px-2 py-0.5 rounded-full bg-cream-dark text-warm-gray font-medium">
                      {sourceLabel(item.source)}
                    </span>
                    <span className="text-xs font-mono text-sage font-bold">{item.score}</span>
                    {item.category && <span className="text-xs text-warm-gray">{item.category}</span>}
                  </div>
                  {item.url ? (
                    <a href={item.url} target="_blank" rel="noopener noreferrer"
                       className="font-medium hover:text-sage transition-colors line-clamp-2">
                      {item.title_ko || item.title}
                    </a>
                  ) : (
                    <p className="font-medium line-clamp-2">{item.title_ko || item.title}</p>
                  )}
                  {item.title_ko && (
                    <p className="text-xs text-warm-gray mt-0.5 line-clamp-1">{item.title}</p>
                  )}
                  {item.score_reasoning && (
                    <p className="text-sm text-warm-gray mt-1 line-clamp-2">{item.score_reasoning}</p>
                  )}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
