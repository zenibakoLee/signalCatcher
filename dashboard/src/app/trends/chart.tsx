"use client";

const COLORS = ["#5C7553", "#D4623A", "#1E3A5F", "#7B68AE", "#6B6560"];

export function TrendChart({ data }: { data: Record<string, { date: string; count: number }[]> }) {
  const keywords = Object.keys(data);
  if (keywords.length === 0) {
    return <p className="text-warm-gray text-sm">차트 데이터 없음</p>;
  }

  const allDates = [...new Set(keywords.flatMap((k) => data[k].map((d) => d.date)))].sort();
  const maxCount = Math.max(...keywords.flatMap((k) => data[k].map((d) => d.count)), 1);

  const width = 800;
  const height = 300;
  const padding = { top: 20, right: 20, bottom: 40, left: 50 };
  const chartW = width - padding.left - padding.right;
  const chartH = height - padding.top - padding.bottom;

  function xPos(dateStr: string) {
    const idx = allDates.indexOf(dateStr);
    return padding.left + (idx / Math.max(allDates.length - 1, 1)) * chartW;
  }
  function yPos(count: number) {
    return padding.top + chartH - (count / maxCount) * chartH;
  }

  return (
    <div className="bg-white rounded-lg border border-light-gray p-4">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto">
        {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
          const y = padding.top + chartH * (1 - frac);
          const val = Math.round(maxCount * frac);
          return (
            <g key={frac}>
              <line x1={padding.left} x2={width - padding.right} y1={y} y2={y} stroke="#E5E1DC" strokeWidth={1} />
              <text x={padding.left - 8} y={y + 4} textAnchor="end" className="fill-warm-gray" fontSize={11}>{val}</text>
            </g>
          );
        })}

        {allDates.filter((_, i) => i % Math.max(Math.floor(allDates.length / 6), 1) === 0).map((d) => (
          <text key={d} x={xPos(d)} y={height - 8} textAnchor="middle" className="fill-warm-gray" fontSize={10}>
            {d.slice(5)}
          </text>
        ))}

        {keywords.map((kw, ki) => {
          const points = data[kw];
          if (points.length < 2) return null;
          const pathD = points.map((p, i) =>
            `${i === 0 ? "M" : "L"} ${xPos(p.date)} ${yPos(p.count)}`
          ).join(" ");
          return (
            <path key={kw} d={pathD} fill="none" stroke={COLORS[ki % COLORS.length]} strokeWidth={2} />
          );
        })}
      </svg>

      <div className="flex flex-wrap gap-4 mt-3 justify-center">
        {keywords.map((kw, ki) => (
          <div key={kw} className="flex items-center gap-1.5 text-xs">
            <span className="w-3 h-3 rounded-full" style={{ backgroundColor: COLORS[ki % COLORS.length] }} />
            {kw}
          </div>
        ))}
      </div>
    </div>
  );
}
