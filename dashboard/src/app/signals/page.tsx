import { getDb } from "@/lib/db";
import type { ScoredItem } from "@/lib/types";
import { SignalFilters } from "./filters";

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

export default async function SignalsPage({ searchParams }: { searchParams: Promise<{ source?: string; category?: string; minScore?: string }> }) {
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

  const items = db.prepare(`
    SELECT s.score, s.score_reasoning, s.category,
           r.id, r.title, r.url, r.source, r.content_snippet, r.published_at, r.metadata
    FROM scored_items s
    JOIN raw_items r ON s.raw_item_id = r.id
    ${where}
    ORDER BY s.score DESC, r.published_at DESC
    LIMIT 100
  `).all(...sqlParams) as ScoredItem[];

  const sources = db.prepare(
    "SELECT DISTINCT r.source FROM scored_items s JOIN raw_items r ON s.raw_item_id = r.id"
  ).all() as { source: string }[];

  const categories = db.prepare(
    "SELECT DISTINCT category FROM scored_items WHERE category IS NOT NULL"
  ).all() as { category: string }[];

  return (
    <div className="space-y-6">
      <h1 className="font-serif text-2xl font-bold">시그널 탐색</h1>

      <SignalFilters
        sources={sources.map((s) => s.source)}
        categories={categories.map((c) => c.category)}
        current={params}
      />

      <p className="text-sm text-warm-gray">{items.length}개 항목</p>

      <div className="grid gap-3 sm:grid-cols-2">
        {items.map((item, i) => (
          <div key={i} className="bg-white rounded-lg border border-light-gray p-4 hover:border-sage/40 transition-colors">
            <div className="flex items-center gap-2 mb-2">
              <span>{scoreEmoji(item.score)}</span>
              <span className="text-xs font-mono text-sage font-bold">{item.score}</span>
              <span className="text-xs px-2 py-0.5 rounded-full bg-cream-dark text-warm-gray">
                {sourceLabel(item.source)}
              </span>
              {item.category && (
                <span className="text-xs px-2 py-0.5 rounded-full bg-sage/10 text-sage">
                  {item.category}
                </span>
              )}
            </div>
            {item.url ? (
              <a href={item.url} target="_blank" rel="noopener noreferrer"
                 className="font-medium text-sm hover:text-sage transition-colors line-clamp-2 block mb-1">
                {item.title}
              </a>
            ) : (
              <p className="font-medium text-sm line-clamp-2 mb-1">{item.title}</p>
            )}
            {item.score_reasoning && (
              <p className="text-xs text-warm-gray line-clamp-2">{item.score_reasoning}</p>
            )}
            <p className="text-xs text-warm-gray/60 mt-2">{item.published_at?.slice(0, 10)}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
