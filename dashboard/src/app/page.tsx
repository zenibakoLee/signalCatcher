import { getDb, kstDateToUtcRange } from "@/lib/db";
import type { ScoredItem, TrendAlert } from "@/lib/types";
import { DatePicker } from "@/components/date-picker";
import {
  ScoreDistribution,
  SourceBreakdown,
  StatCard,
  Sparkline,
  CategoryHeatmap,
} from "@/components/charts";
import { ExpandableText } from "@/components/expandable-text";

export const dynamic = "force-dynamic";

const SOURCE_COLORS: Record<string, { label: string; color: string }> = {
  hackernews: { label: "HN", color: "#FF6600" },
  arxiv: { label: "arXiv", color: "#B31B1B" },
  github: { label: "GitHub", color: "#6e40c9" },
  rss: { label: "RSS", color: "#1E3A5F" },
  youtube: { label: "YouTube", color: "#FF0000" },
  reddit: { label: "Reddit", color: "#FF5700" },
  apewisdom: { label: "ApeWisdom", color: "#D4623A" },
};

function scoreEmoji(score: number) {
  if (score >= 90) return "🔴";
  if (score >= 70) return "🟡";
  return "🟢";
}

function sourceLabel(source: string) {
  return SOURCE_COLORS[source]?.label || source;
}

interface Digest {
  digest_date: string;
  headline: string;
  summary_md: string;
}

export default async function Home({
  searchParams,
}: {
  searchParams: Promise<{ date?: string }>;
}) {
  const { date } = await searchParams;
  const db = getDb();

  const availableDates = (
    db
      .prepare("SELECT digest_date FROM digests ORDER BY digest_date ASC")
      .all() as { digest_date: string }[]
  ).map((r) => r.digest_date);

  if (availableDates.length === 0) {
    return (
      <div className="text-center py-20">
        <h1 className="font-serif text-3xl font-bold mb-4">시그널 캐처</h1>
        <p className="text-warm-gray">아직 생성된 다이제스트가 없습니다.</p>
      </div>
    );
  }

  const targetDate =
    date && availableDates.includes(date)
      ? date
      : availableDates[availableDates.length - 1];

  const digest = db
    .prepare("SELECT * FROM digests WHERE digest_date = ?")
    .get(targetDate) as Digest;

  const [utcStart, utcEnd] = kstDateToUtcRange(targetDate);

  const topItems = db
    .prepare(
      `
    WITH ranked AS (
      SELECT s.score, s.score_reasoning, s.category, s.title_ko, s.related_tickers,
             r.title, r.url, r.source, r.content_snippet,
             ROW_NUMBER() OVER (PARTITION BY r.source ORDER BY s.score DESC) as rn
      FROM scored_items s
      JOIN raw_items r ON s.raw_item_id = r.id
      WHERE r.collected_at >= ? AND r.collected_at < ?
    )
    SELECT score, score_reasoning, category, title_ko, related_tickers, title, url, source, content_snippet
    FROM ranked WHERE rn <= 3
    ORDER BY score DESC LIMIT 10
  `
    )
    .all(utcStart, utcEnd) as ScoredItem[];

  const alerts = db
    .prepare(
      "SELECT * FROM trend_alerts WHERE alert_date = ? ORDER BY z_score DESC"
    )
    .all(targetDate) as TrendAlert[];

  // ── Stats for visual dashboard ──────────────────────────────────────────
  const allDayItems = db
    .prepare(
      `
    SELECT s.score, r.source, s.category
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    WHERE r.collected_at >= ? AND r.collected_at < ?
  `
    )
    .all(utcStart, utcEnd) as { score: number; source: string; category: string | null }[];

  const totalSignals = allDayItems.length;
  const highScoreCount = allDayItems.filter((i) => i.score >= 70).length;
  const avgScore =
    totalSignals > 0
      ? Math.round(allDayItems.reduce((s, i) => s + i.score, 0) / totalSignals)
      : 0;

  // Score distribution
  const scoreBuckets = [
    { label: "90+", count: allDayItems.filter((i) => i.score >= 90).length, color: "#C0392B" },
    { label: "70–89", count: allDayItems.filter((i) => i.score >= 70 && i.score < 90).length, color: "#D4623A" },
    { label: "50–69", count: allDayItems.filter((i) => i.score >= 50 && i.score < 70).length, color: "#5C7553" },
    { label: "< 50", count: allDayItems.filter((i) => i.score < 50).length, color: "#A8C49E" },
  ];

  // Source breakdown
  const sourceCounts = new Map<string, number>();
  for (const item of allDayItems) {
    sourceCounts.set(item.source, (sourceCounts.get(item.source) || 0) + 1);
  }
  const sourceStats = [...sourceCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([source, count]) => ({
      source,
      label: SOURCE_COLORS[source]?.label || source,
      count,
      color: SOURCE_COLORS[source]?.color || "#6B6560",
    }));

  // Category stats
  const categoryMap = new Map<string, { count: number; totalScore: number }>();
  for (const item of allDayItems) {
    const cat = item.category || "미분류";
    const prev = categoryMap.get(cat) || { count: 0, totalScore: 0 };
    categoryMap.set(cat, { count: prev.count + 1, totalScore: prev.totalScore + item.score });
  }
  const categoryStats = [...categoryMap.entries()]
    .map(([category, { count, totalScore }]) => ({
      category,
      count,
      avgScore: Math.round(totalScore / count),
    }))
    .sort((a, b) => b.count - a.count)
    .slice(0, 12);

  // Sparkline data for trend alerts
  const alertSparklines: Record<string, number[]> = {};
  for (const a of alerts.slice(0, 6)) {
    const rows = db
      .prepare(
        `
      SELECT total_count FROM keyword_daily_aggregates
      WHERE keyword = ? AND mention_date <= ?
      ORDER BY mention_date DESC LIMIT 14
    `
      )
      .all(a.keyword, targetDate) as { total_count: number }[];
    alertSparklines[a.keyword] = rows.map((r) => r.total_count).reverse();
  }

  return (
    <div className="space-y-8">
      <header className="border-b border-light-gray pb-6">
        <div className="relative mb-3">
          <DatePicker currentDate={targetDate} availableDates={availableDates} />
        </div>
        <h1 className="font-serif text-3xl font-bold leading-tight mb-3">
          {digest.headline}
        </h1>
      </header>

      {/* ── Visual Dashboard ─────────────────────────────────────────────── */}
      <section className="grid grid-cols-3 gap-4">
        <StatCard label="수집 시그널" value={totalSignals} sub={`${highScoreCount}개 고스코어 (70+)`} />
        <StatCard label="평균 점수" value={avgScore} accent={avgScore >= 70 ? "#D4623A" : "#5C7553"} />
        <StatCard label="트렌드 알림" value={alerts.length} sub={alerts.filter((a) => a.severity === "urgent").length + "건 긴급"} accent={alerts.length > 0 ? "#C0392B" : "#5C7553"} />
      </section>

      <section className="grid grid-cols-1 md:grid-cols-2 gap-6">
        <div className="bg-white rounded-lg border border-light-gray p-4">
          <h3 className="text-sm font-bold text-charcoal mb-3">점수 분포</h3>
          <ScoreDistribution buckets={scoreBuckets} />
        </div>
        <div className="bg-white rounded-lg border border-light-gray p-4">
          <h3 className="text-sm font-bold text-charcoal mb-3">소스별 수집 현황</h3>
          <SourceBreakdown stats={sourceStats} />
        </div>
      </section>

      {categoryStats.length > 0 && (
        <section className="bg-white rounded-lg border border-light-gray p-4">
          <h3 className="text-sm font-bold text-charcoal mb-3">카테고리 히트맵</h3>
          <p className="text-xs text-warm-gray mb-2">크기 = 건수 · 색상 = 평균 스코어 (빨강 &gt; 주황 &gt; 녹색)</p>
          <CategoryHeatmap categories={categoryStats} />
        </section>
      )}

      {/* ── Trend Alerts ─────────────────────────────────────────────────── */}
      {alerts.length > 0 && (
        <section>
          <h2 className="font-serif text-xl font-bold mb-4 text-ember">
            📈 트렌드 알림
          </h2>
          <div className="grid gap-3">
            {alerts.map((a) => (
              <div
                key={a.keyword}
                className="bg-white border border-ember/20 rounded-lg p-4"
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-3">
                    <span className="font-bold">{a.keyword}</span>
                    {alertSparklines[a.keyword] && (
                      <Sparkline
                        data={alertSparklines[a.keyword]}
                        color={a.severity === "urgent" ? "#C0392B" : "#D4623A"}
                        width={100}
                        height={28}
                      />
                    )}
                  </div>
                  <span
                    className={`px-2 py-0.5 rounded-full text-xs font-medium text-white ${
                      a.severity === "urgent" ? "bg-red-alert" : "bg-ember"
                    }`}
                  >
                    {a.severity === "urgent" ? "긴급" : "주목"} · z={a.z_score}
                  </span>
                </div>
                <p className="text-sm text-warm-gray">
                  오늘 {a.today_count}회 vs 30일 평균{" "}
                  {a.moving_avg_30d?.toFixed(1)}회
                </p>
                {a.llm_interpretation && (
                  <p className="text-sm mt-2">{a.llm_interpretation}</p>
                )}
              </div>
            ))}
          </div>
        </section>
      )}

      {/* ── Top 10 Signals ───────────────────────────────────────────────── */}
      <section>
        <h2 className="font-serif text-xl font-bold mb-4">시그널 Top 10</h2>
        <div className="space-y-3">
          {topItems.map((item, i) => (
            <div
              key={i}
              className="bg-white rounded-lg border border-light-gray p-4 hover:border-sage/40 transition-colors"
            >
              <div className="flex items-start gap-3">
                <div className="flex flex-col items-center gap-1">
                  <span className="text-lg">{scoreEmoji(item.score)}</span>
                  {/* Score bar */}
                  <div className="w-1.5 h-10 bg-cream-dark rounded-full overflow-hidden">
                    <div
                      className="w-full rounded-full transition-all"
                      style={{
                        height: `${item.score}%`,
                        backgroundColor:
                          item.score >= 90
                            ? "#C0392B"
                            : item.score >= 70
                              ? "#D4623A"
                              : "#5C7553",
                        marginTop: `${100 - item.score}%`,
                      }}
                    />
                  </div>
                </div>
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 mb-1">
                    <span className="text-xs px-2 py-0.5 rounded-full bg-cream-dark text-warm-gray font-medium">
                      {sourceLabel(item.source)}
                    </span>
                    <span className="text-xs font-mono text-sage font-bold">
                      {item.score}
                    </span>
                    {item.category && (
                      <span className="text-xs text-warm-gray">
                        {item.category}
                      </span>
                    )}
                  </div>
                  {item.url ? (
                    <a
                      href={item.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="font-medium hover:text-sage transition-colors sm:line-clamp-2"
                    >
                      {item.title_ko || item.title}
                    </a>
                  ) : (
                    <p className="font-medium sm:line-clamp-2">
                      {item.title_ko || item.title}
                    </p>
                  )}
                  {item.title_ko && (
                    <ExpandableText lines={1} className="text-xs text-warm-gray mt-0.5">
                      {item.title}
                    </ExpandableText>
                  )}
                  {item.score_reasoning && (
                    <ExpandableText lines={2} className="text-sm text-warm-gray mt-1">
                      {item.score_reasoning}
                    </ExpandableText>
                  )}
                  {item.related_tickers && (() => {
                    try {
                      const tickers = JSON.parse(item.related_tickers) as string[];
                      if (tickers.length === 0) return null;
                      return (
                        <div className="flex items-center gap-1.5 mt-2 flex-wrap">
                          <span className="text-xs text-warm-gray">관련종목</span>
                          {tickers.map((t) => (
                            <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-ember/10 text-ember font-medium">{t}</span>
                          ))}
                        </div>
                      );
                    } catch { return null; }
                  })()}
                </div>
              </div>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
