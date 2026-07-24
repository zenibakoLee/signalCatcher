import { getDb } from "@/lib/db";

export const dynamic = "force-dynamic";

type Thesis = {
  id: number;
  thesis_date: string;
  direction: string;
  company: string;
  ticker: string | null;
  market: string | null;
  bottleneck: string | null;
  reasoning: string;
  depth_layer: number | null;
  pricing_status: string | null;
  conviction: string | null;
  falsifier: string | null;
};

const FLAG: Record<string, string> = { US: "🇺🇸", KR: "🇰🇷", JP: "🇯🇵" };
const PRICING: Record<string, { label: string; cls: string }> = {
  unpriced: { label: "미반영", cls: "bg-sage/15 text-sage" },
  partial: { label: "부분반영", cls: "bg-yellow-100 text-yellow-700" },
  mostly: { label: "상당반영", cls: "bg-orange-100 text-orange-700" },
  overpriced: { label: "과열", cls: "bg-red-100 text-red-alert" },
};
const CONV: Record<string, string> = { high: "🔥", medium: "▪️", low: "·" };
const LAYERS: Record<number, { title: string; desc: string }> = {
  1: { title: "🥇 1층 — 최심부", desc: "대체 거의 불가능 · 다년 리드타임·과점" },
  2: { title: "🥈 2층 — 중간", desc: "유의미한 해자, 시간이 지나면 경쟁 위험" },
  3: { title: "🥉 3층 — 표층", desc: "지금 수혜, 진입장벽 낮아 상품화 위험" },
};

function ThesisCard({ t }: { t: Thesis }) {
  const price = t.pricing_status ? PRICING[t.pricing_status] : null;
  return (
    <div className="bg-white rounded-lg border border-light-gray p-4">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 min-w-0">
          <span>{FLAG[t.market ?? ""] ?? ""}</span>
          <span className="font-bold truncate">{t.company}</span>
          {t.ticker && <span className="font-mono text-xs text-warm-gray">{t.ticker}</span>}
          <span title="확신도">{CONV[t.conviction ?? ""] ?? ""}</span>
        </div>
        {price && (
          <span className={`text-xs px-2 py-0.5 rounded-full shrink-0 ${price.cls}`}>{price.label}</span>
        )}
      </div>
      {t.bottleneck && <p className="text-sm font-semibold mt-2">{t.bottleneck}</p>}
      <p className="text-xs text-warm-gray mt-1 leading-relaxed">{t.reasoning}</p>
      {t.falsifier && (
        <p className="text-xs text-charcoal/60 mt-2 border-t border-light-gray pt-2">
          ↩︎ 반증: {t.falsifier}
        </p>
      )}
    </div>
  );
}

export default async function ThesesPage() {
  const db = getDb();
  const latest = db
    .prepare("SELECT MAX(thesis_date) as d FROM investment_theses")
    .get() as { d: string | null };

  const theses = latest?.d
    ? (db
        .prepare(
          "SELECT * FROM investment_theses WHERE thesis_date = ? ORDER BY id DESC"
        )
        .all(latest.d) as Thesis[])
    : [];

  const buys = theses.filter((t) => t.direction === "buy");
  const avoids = theses.filter((t) => t.direction === "avoid");

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h1 className="font-serif text-2xl font-bold">종목 발굴</h1>
        {latest?.d && <span className="text-xs text-warm-gray">{latest.d} 기준</span>}
      </div>
      <p className="text-sm text-warm-gray">
        수집된 강력 시그널로부터 2차적 추론을 통해 발굴한 투자 대상입니다. 병목의 깊이(대체
        불가능성)로 3개 층으로 나누고, 각 항목에 가격 반영 정도를 표기합니다.
      </p>

      {theses.length === 0 ? (
        <div className="bg-white rounded-lg border border-light-gray p-8 text-center text-warm-gray">
          아직 발굴 결과가 없습니다. 매일 다이제스트 직후 자동 생성됩니다.
        </div>
      ) : (
        <>
          <section className="space-y-4">
            <h2 className="font-serif text-lg font-bold text-sage">🎯 매수 발굴 — 병목 깊이순</h2>
            {[1, 2, 3].map((layer) => {
              const items = buys.filter((t) => t.depth_layer === layer);
              if (items.length === 0) return null;
              return (
                <div key={layer} className="space-y-2">
                  <div className="flex items-baseline gap-2">
                    <span className="font-semibold text-sm">{LAYERS[layer].title}</span>
                    <span className="text-xs text-warm-gray">{LAYERS[layer].desc}</span>
                  </div>
                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    {items.map((t) => (
                      <ThesisCard key={t.id} t={t} />
                    ))}
                  </div>
                </div>
              );
            })}
            {buys.filter((t) => !t.depth_layer || t.depth_layer < 1 || t.depth_layer > 3).length > 0 && (
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {buys
                  .filter((t) => !t.depth_layer || t.depth_layer < 1 || t.depth_layer > 3)
                  .map((t) => (
                    <ThesisCard key={t.id} t={t} />
                  ))}
              </div>
            )}
          </section>

          {avoids.length > 0 && (
            <section className="space-y-2">
              <h2 className="font-serif text-lg font-bold text-ember">
                🛑 회피·청산 후보 — 과도기 프리미엄 회귀
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                {avoids.map((t) => (
                  <ThesisCard key={t.id} t={t} />
                ))}
              </div>
            </section>
          )}
        </>
      )}
    </div>
  );
}
