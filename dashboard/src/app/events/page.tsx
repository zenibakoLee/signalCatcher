import { getDb } from "@/lib/db";
import type { ConferenceBriefing } from "@/lib/types";

export const dynamic = "force-dynamic";

interface RelatedItem {
  title: string;
  url: string | null;
  source: string;
  score: number | null;
}

export default function EventsPage() {
  const db = getDb();

  const briefings = db.prepare(`
    SELECT id, conference_name, conference_start, conference_end,
           briefing_type, content_md, expected_items, silent_signals, source_item_ids, generated_at
    FROM conference_briefings
    ORDER BY conference_start DESC, briefing_type DESC
  `).all() as ConferenceBriefing[];

  if (briefings.length === 0) {
    return (
      <div className="space-y-6">
        <h1 className="font-serif text-2xl font-bold">컨퍼런스 브리핑</h1>
        <p className="text-warm-gray">아직 생성된 브리핑이 없습니다.</p>
      </div>
    );
  }

  const grouped: Record<string, ConferenceBriefing[]> = {};
  for (const b of briefings) {
    const key = `${b.conference_name}|${b.conference_start}`;
    if (!grouped[key]) grouped[key] = [];
    grouped[key].push(b);
  }

  return (
    <div className="space-y-8">
      <h1 className="font-serif text-2xl font-bold">컨퍼런스 브리핑</h1>

      {Object.entries(grouped).map(([key, items]) => {
        const first = items[0];
        return (
          <section key={key} className="bg-white rounded-lg border border-light-gray overflow-hidden">
            <div className="bg-deep-blue text-white p-4">
              <h2 className="font-serif text-xl font-bold">{first.conference_name}</h2>
              <p className="text-sm text-white/70 mt-1">
                {first.conference_start} ~ {first.conference_end}
              </p>
            </div>

            <div className="divide-y divide-light-gray">
              {items.map((b) => {
                let relatedItems: RelatedItem[] = [];
                if (b.briefing_type === "post_event") {
                  try {
                    const ids: number[] = b.source_item_ids
                      ? JSON.parse(b.source_item_ids as unknown as string)
                      : [];
                    if (ids.length > 0) {
                      relatedItems = db.prepare(`
                        SELECT r.title, r.url, r.source, s.score
                        FROM raw_items r
                        LEFT JOIN scored_items s ON s.raw_item_id = r.id
                        WHERE r.id IN (${ids.map(() => "?").join(",")})
                        ORDER BY COALESCE(s.score, 0) DESC
                        LIMIT 10
                      `).all(...ids) as RelatedItem[];
                    }
                  } catch {}
                }
                if (relatedItems.length === 0) {
                  try {
                    relatedItems = db.prepare(`
                      SELECT r.title, r.url, r.source, s.score
                      FROM raw_items r
                      LEFT JOIN scored_items s ON s.raw_item_id = r.id
                      WHERE r.published_at >= ? AND r.published_at <= date(?, '+1 day')
                        AND (r.title LIKE '%' || ? || '%')
                      ORDER BY COALESCE(s.score, 0) DESC
                      LIMIT 8
                    `).all(b.conference_start, b.conference_end, b.conference_name.split(" ")[0]) as RelatedItem[];
                  } catch {}
                }
                return (
                  <BriefingCard key={b.id} briefing={b} relatedItems={relatedItems} />
                );
              })}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function BriefingCard({ briefing, relatedItems = [] }: { briefing: ConferenceBriefing; relatedItems?: RelatedItem[] }) {
  const isPre = briefing.briefing_type === "pre_event";
  const label = isPre ? "📋 사전 브리핑" : "📊 사후 브리핑";

  let expectedItems: { item: string; investment_relevance?: string }[] = [];
  if (briefing.expected_items) {
    try { expectedItems = JSON.parse(briefing.expected_items); } catch {}
  }

  let silentSignals: { expected_item: string; interpretation?: string }[] = [];
  if (briefing.silent_signals) {
    try { silentSignals = JSON.parse(briefing.silent_signals); } catch {}
  }

  const summary = briefing.content_md.split("\n").filter((l) => l && !l.startsWith("#")).slice(0, 3).join(" ");

  return (
    <div className="p-4 space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="font-bold">{label}</h3>
        <span className="text-xs text-warm-gray">{briefing.generated_at?.slice(0, 10)}</span>
      </div>

      <p className="text-sm text-charcoal/80 line-clamp-3">{summary}</p>

      {expectedItems.length > 0 && (
        <div>
          <h4 className="text-xs font-bold text-warm-gray mb-1 uppercase tracking-wide">
            {isPre ? "예상 항목" : "사전 예상 항목"}
          </h4>
          <div className="flex flex-wrap gap-1.5">
            {expectedItems.slice(0, 8).map((e, i) => {
              const rel = e.investment_relevance;
              const bg = rel === "높음" ? "bg-red-alert/10 text-red-alert" : rel === "중간" ? "bg-ember/10 text-ember" : "bg-sage/10 text-sage";
              return (
                <span key={i} className={`text-xs px-2 py-1 rounded-full ${bg}`}>
                  {e.item}
                </span>
              );
            })}
          </div>
        </div>
      )}

      {silentSignals.length > 0 && (
        <div>
          <h4 className="text-xs font-bold text-warm-gray mb-1 uppercase tracking-wide">🔇 Silent Signals</h4>
          <div className="space-y-1">
            {silentSignals.map((s, i) => (
              <div key={i} className="text-sm">
                <span className="font-medium">{s.expected_item}</span>
                {s.interpretation && (
                  <span className="text-warm-gray"> — {s.interpretation}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}

      {relatedItems.length > 0 && (
        <div>
          <h4 className="text-xs font-bold text-warm-gray mb-1 uppercase tracking-wide">🔗 관련 수집 콘텐츠</h4>
          <div className="space-y-1.5">
            {relatedItems.map((item, i) => (
              <div key={i} className="text-sm flex items-start gap-2">
                <span className="text-xs px-1.5 py-0.5 rounded bg-cream-dark text-warm-gray shrink-0 mt-0.5">
                  {item.source === "hackernews" ? "HN" : item.source === "github" ? "GH" : item.source}
                </span>
                {item.url ? (
                  <a href={item.url} target="_blank" rel="noopener noreferrer"
                     className="text-deep-blue hover:underline line-clamp-1">
                    {item.title}
                  </a>
                ) : (
                  <span className="line-clamp-1">{item.title}</span>
                )}
                {item.score && (
                  <span className="text-xs text-sage font-mono shrink-0">{item.score}</span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
