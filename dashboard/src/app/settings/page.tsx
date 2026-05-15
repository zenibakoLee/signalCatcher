import { getDb } from "@/lib/db";
import type { Keyword } from "@/lib/types";

export const dynamic = "force-dynamic";

const categoryLabels: Record<string, string> = {
  ai_model: "AI 모델",
  hardware: "하드웨어",
  framework: "프레임워크",
  concept: "개념",
  company: "기업",
  infrastructure: "인프라",
};

const statusLabels: Record<string, { label: string; color: string }> = {
  active: { label: "활성", color: "bg-sage text-white" },
  suggested: { label: "제안됨", color: "bg-lavender text-white" },
  rejected: { label: "거부", color: "bg-warm-gray text-white" },
  retired: { label: "은퇴", color: "bg-light-gray text-charcoal" },
};

export default function SettingsPage() {
  const db = getDb();

  const keywords = db.prepare(
    "SELECT * FROM keywords ORDER BY status, category, keyword"
  ).all() as Keyword[];

  const byCategory: Record<string, Keyword[]> = {};
  for (const kw of keywords) {
    const cat = kw.category || "기타";
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(kw);
  }

  const stats = {
    total: keywords.length,
    active: keywords.filter((k) => k.status === "active").length,
    suggested: keywords.filter((k) => k.status === "suggested").length,
  };

  const runs = db.prepare(`
    SELECT run_type, started_at, status, items_collected, items_scored, duration_secs
    FROM pipeline_runs
    ORDER BY started_at DESC
    LIMIT 10
  `).all() as {
    run_type: string; started_at: string; status: string;
    items_collected: number; items_scored: number; duration_secs: number;
  }[];

  return (
    <div className="space-y-8">
      <h1 className="font-serif text-2xl font-bold">설정</h1>

      <section>
        <div className="grid grid-cols-3 gap-4">
          <div className="bg-white rounded-lg border border-light-gray p-4 text-center">
            <p className="text-3xl font-bold text-sage">{stats.active}</p>
            <p className="text-sm text-warm-gray">활성 키워드</p>
          </div>
          <div className="bg-white rounded-lg border border-light-gray p-4 text-center">
            <p className="text-3xl font-bold text-lavender">{stats.suggested}</p>
            <p className="text-sm text-warm-gray">제안된 키워드</p>
          </div>
          <div className="bg-white rounded-lg border border-light-gray p-4 text-center">
            <p className="text-3xl font-bold text-charcoal">{stats.total}</p>
            <p className="text-sm text-warm-gray">전체</p>
          </div>
        </div>
      </section>

      <section>
        <h2 className="font-serif text-lg font-bold mb-3">키워드 관리</h2>
        {Object.entries(byCategory).map(([cat, kws]) => (
          <div key={cat} className="mb-4">
            <h3 className="text-sm font-bold text-warm-gray mb-2">
              {categoryLabels[cat] || cat}
            </h3>
            <div className="flex flex-wrap gap-2">
              {kws.map((kw) => {
                const st = statusLabels[kw.status] || statusLabels.active;
                return (
                  <span
                    key={kw.id}
                    className={`px-3 py-1 rounded-full text-xs font-medium ${st.color}`}
                  >
                    {kw.keyword}
                  </span>
                );
              })}
            </div>
          </div>
        ))}
      </section>

      <section>
        <h2 className="font-serif text-lg font-bold mb-3">파이프라인 실행 이력</h2>
        <div className="bg-white rounded-lg border border-light-gray overflow-hidden">
          <table className="w-full text-sm">
            <thead>
              <tr className="border-b border-light-gray bg-cream-dark/50">
                <th className="text-left p-3 font-medium">유형</th>
                <th className="text-left p-3 font-medium">시작</th>
                <th className="text-left p-3 font-medium">상태</th>
                <th className="text-right p-3 font-medium">수집</th>
                <th className="text-right p-3 font-medium">스코어</th>
                <th className="text-right p-3 font-medium">소요</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run, i) => (
                <tr key={i} className="border-b border-light-gray/50">
                  <td className="p-3 font-medium">{run.run_type}</td>
                  <td className="p-3 text-warm-gray text-xs">{run.started_at?.slice(0, 16)}</td>
                  <td className="p-3">
                    <span className={`px-2 py-0.5 rounded-full text-xs ${
                      run.status === "completed" ? "bg-sage/10 text-sage" :
                      run.status === "failed" ? "bg-red-alert/10 text-red-alert" :
                      "bg-ember/10 text-ember"
                    }`}>
                      {run.status}
                    </span>
                  </td>
                  <td className="p-3 text-right font-mono">{run.items_collected || 0}</td>
                  <td className="p-3 text-right font-mono">{run.items_scored || 0}</td>
                  <td className="p-3 text-right text-warm-gray">{run.duration_secs?.toFixed(1)}s</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
