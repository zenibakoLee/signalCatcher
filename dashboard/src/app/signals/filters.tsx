"use client";

export function SignalFilters({
  sources,
  categories,
  current,
}: {
  sources: string[];
  categories: string[];
  current: { source?: string; category?: string; minScore?: string };
}) {
  return (
    <form action="/signals" method="GET" className="flex flex-wrap gap-3">
      <select
        name="source"
        defaultValue={current.source || ""}
        onChange={(e) => e.target.form?.submit()}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">모든 소스</option>
        {sources.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>

      <select
        name="category"
        defaultValue={current.category || ""}
        onChange={(e) => e.target.form?.submit()}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">모든 카테고리</option>
        {categories.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>

      <select
        name="minScore"
        defaultValue={current.minScore || ""}
        onChange={(e) => e.target.form?.submit()}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">최소 점수 없음</option>
        <option value="50">50+</option>
        <option value="70">70+</option>
        <option value="85">85+</option>
      </select>

      <noscript>
        <button type="submit" className="text-sm px-3 py-1.5 rounded-full bg-sage text-white">적용</button>
      </noscript>
    </form>
  );
}
