import { getDb } from "@/lib/db";
import type { ScoredItem } from "@/lib/types";
import { SignalFilters } from "./filters";
import { ScoreDistribution, SourceBreakdown } from "@/components/charts";
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

export default async function SignalsPage({
  searchParams,
}: {
  searchParams: Promise<{
    source?: string;
    category?: string;
    minScore?: string;
  }>;
}) {
  const params = await searchParams;
  const db = getDb();

  let where = "WHERE 1=1";
  const sqlParams: (string | number)[] = [];

  if (params.source) {
    where += " AND r.source = ?";
    sqlParams.push(params.source);
  }
  if (params.category) {
    where += " AND s.category = ?";
    sqlParams.push(params.category);
  }
  const minScore = parseInt(params.minScore || "0");
  if (minScore > 0) {
    where += " AND s.score >= ?";
    sqlParams.push(minScore);
  }

  const items = db
    .prepare(
      `
    SELECT s.score, s.score_reasoning, s.category, s.title_ko, s.related_tickers,
           r.id, r.title, r.url, r.source, r.content_snippet, r.published_at, r.metadata
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    ${where}
    ORDER BY s.score DESC, r.published_at DESC
    LIMIT 100
  `
    )
    .all(...sqlParams) as ScoredItem[];

  const sources = db
    .prepare(
      "SELECT DISTINCT r.source FROM scored_items s JOIN raw_items r ON s.raw_item_id = r.id"
    )
    .all() as { source: string }[];

  const categories = db
    .prepare(
      "SELECT DISTINCT category FROM scored_items WHERE category IS NOT NULL"
    )
    .all() as { category: string }[];

  // ── Visual summary stats ────────────────────────────────────────────────
  const scoreBuckets = [
    { label: "90+", count: items.filter((i) => i.score >= 90).length, color: "#C0392B" },
    { label: "70–89", count: items.filter((i) => i.score >= 70 && i.score < 90).length, color: "#D4623A" },
    { label: "50–69", count: items.filter((i) => i.score >= 50 && i.score < 70).length, color: "#5C7553" },
    { label: "< 50", count: items.filter((i) => i.score < 50).length, color: "#A8C49E" },
  ];

  const sourceCounts = new Map<string, number>();
  for (const item of items) {
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

  return (
    <div className="space-y-6">
      <h1 className="font-serif text-2xl font-bold">시그널 탐색</h1>

      <SignalFilters
        sources={sources.map((s) => s.source)}
        categories={categories.map((c) => c.category)}
        current={params}
      />

      {/* Visual summary */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="bg-white rounded-lg border border-light-gray p-4">
          <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-2">점수 분포</h3>
          <ScoreDistribution buckets={scoreBuckets} />
        </div>
        <div className="bg-white rounded-lg border border-light-gray p-4">
          <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-2">소스 분포</h3>
          <SourceBreakdown stats={sourceStats} />
        </div>
      </div>

      <p className="text-sm text-warm-gray">{items.length}개 항목</p>

      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((item, i) => (
          <div
            key={i}
            className="bg-white rounded-lg border border-light-gray p-4 hover:border-sage/40 transition-colors"
          >
            <div className="flex items-center gap-2 mb-2">
              <span>{scoreEmoji(item.score)}</span>
              <span className="text-xs font-mono text-sage font-bold">
                {item.score}
              </span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-cream-dark text-warm-gray">
                {sourceLabel(item.source)}
              </span>
              {item.category && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-sage/10 text-sage">
                  {item.category}
                </span>
              )}
              {/* Mini score bar */}
              <div className="flex-1" />
              <div className="w-16 h-1.5 bg-cream-dark rounded-full overflow-hidden">
                <div
                  className="h-full rounded-full"
                  style={{
                    width: `${item.score}%`,
                    backgroundColor:
                      item.score >= 90 ? "#C0392B" : item.score >= 70 ? "#D4623A" : "#5C7553",
                  }}
                />
              </div>
            </div>
            {item.url ? (
              <a
                href={item.url}
                target="_blank"
                rel="noopener noreferrer"
                className="font-medium text-sm hover:text-sage transition-colors sm:line-clamp-2 block mb-1"
              >
                {item.title_ko || item.title}
              </a>
            ) : (
              <p className="font-medium text-sm sm:line-clamp-2 mb-1">
                {item.title_ko || item.title}
              </p>
            )}
            {item.title_ko && (
              <ExpandableText lines={1} className="text-xs text-warm-gray mb-1">
                {item.title}
              </ExpandableText>
            )}
            {item.score_reasoning && (
              <ExpandableText lines={2} className="text-xs text-warm-gray">
                {item.score_reasoning}
              </ExpandableText>
            )}
            <div className="flex items-center gap-2 mt-2 flex-wrap">
              <span className="text-xs text-warm-gray/60">
                {item.published_at?.slice(0, 10)}
              </span>
              {item.related_tickers && (() => {
                try {
                  const tickers = JSON.parse(item.related_tickers) as string[];
                  if (tickers.length === 0) return null;
                  return tickers.map((t) => (
                    <span key={t} className="text-xs px-1.5 py-0.5 rounded bg-ember/10 text-ember font-medium">{t}</span>
                  ));
                } catch { return null; }
              })()}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
