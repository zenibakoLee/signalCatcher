"use client";

// ── Score Distribution ──────────────────────────────────────────────────────

interface ScoreBucket {
  label: string;
  count: number;
  color: string;
}

export function ScoreDistribution({ buckets }: { buckets: ScoreBucket[] }) {
  const max = Math.max(...buckets.map((b) => b.count), 1);
  const total = buckets.reduce((s, b) => s + b.count, 0);

  return (
    <div className="space-y-1.5">
      {buckets.map((b) => (
        <div key={b.label} className="flex items-center gap-2">
          <span className="text-xs text-warm-gray w-12 text-right shrink-0">{b.label}</span>
          <div className="flex-1 h-5 bg-cream-dark rounded-full overflow-hidden">
            <div
              className="h-full rounded-full transition-all duration-500"
              style={{
                width: `${(b.count / max) * 100}%`,
                backgroundColor: b.color,
                minWidth: b.count > 0 ? "4px" : "0",
              }}
            />
          </div>
          <span className="text-xs font-mono w-8 text-right text-warm-gray">{b.count}</span>
        </div>
      ))}
      <p className="text-xs text-warm-gray text-right">총 {total}개</p>
    </div>
  );
}

// ── Source Breakdown ─────────────────────────────────────────────────────────

interface SourceStat {
  source: string;
  label: string;
  count: number;
  color: string;
}

export function SourceBreakdown({ stats }: { stats: SourceStat[] }) {
  const total = stats.reduce((s, st) => s + st.count, 0);
  if (total === 0) return null;

  return (
    <div className="space-y-2">
      {/* Stacked bar */}
      <div className="h-6 rounded-full overflow-hidden flex">
        {stats.map((s) => (
          <div
            key={s.source}
            className="h-full transition-all duration-500 relative group"
            style={{
              width: `${(s.count / total) * 100}%`,
              backgroundColor: s.color,
              minWidth: s.count > 0 ? "3px" : "0",
            }}
            title={`${s.label}: ${s.count}`}
          />
        ))}
      </div>
      {/* Legend */}
      <div className="flex flex-wrap gap-x-4 gap-y-1">
        {stats.filter((s) => s.count > 0).map((s) => (
          <div key={s.source} className="flex items-center gap-1.5 text-xs">
            <span className="w-2.5 h-2.5 rounded-full shrink-0" style={{ backgroundColor: s.color }} />
            <span className="text-warm-gray">{s.label}</span>
            <span className="font-mono text-charcoal">{s.count}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ── Sparkline ───────────────────────────────────────────────────────────────

export function Sparkline({
  data,
  color = "#5C7553",
  width = 120,
  height = 32,
}: {
  data: number[];
  color?: string;
  width?: number;
  height?: number;
}) {
  if (data.length < 2) return null;

  const max = Math.max(...data, 1);
  const min = Math.min(...data, 0);
  const range = max - min || 1;
  const pad = 2;

  const points = data.map((v, i) => {
    const x = pad + (i / (data.length - 1)) * (width - pad * 2);
    const y = pad + (1 - (v - min) / range) * (height - pad * 2);
    return `${x},${y}`;
  });

  const areaPoints = [
    `${pad},${height - pad}`,
    ...points,
    `${width - pad},${height - pad}`,
  ];

  return (
    <svg width={width} height={height} className="shrink-0">
      <polygon
        points={areaPoints.join(" ")}
        fill={color}
        opacity={0.12}
      />
      <polyline
        points={points.join(" ")}
        fill="none"
        stroke={color}
        strokeWidth={1.5}
        strokeLinejoin="round"
        strokeLinecap="round"
      />
      {/* Last dot */}
      {points.length > 0 && (
        <circle
          cx={parseFloat(points[points.length - 1].split(",")[0])}
          cy={parseFloat(points[points.length - 1].split(",")[1])}
          r={2.5}
          fill={color}
        />
      )}
    </svg>
  );
}

// ── Stat Card ───────────────────────────────────────────────────────────────

export function StatCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string;
  value: string | number;
  sub?: string;
  accent?: string;
}) {
  return (
    <div className="bg-white rounded-lg border border-light-gray p-4">
      <p className="text-xs text-warm-gray uppercase tracking-wide mb-1">{label}</p>
      <p className="text-2xl font-bold" style={accent ? { color: accent } : undefined}>
        {value}
      </p>
      {sub && <p className="text-xs text-warm-gray mt-0.5">{sub}</p>}
    </div>
  );
}

// ── Category Heatmap ────────────────────────────────────────────────────────

interface CategoryStat {
  category: string;
  count: number;
  avgScore: number;
}

export function CategoryHeatmap({ categories }: { categories: CategoryStat[] }) {
  if (categories.length === 0) return null;
  const maxCount = Math.max(...categories.map((c) => c.count), 1);

  return (
    <div className="flex flex-wrap gap-1.5">
      {categories.map((c) => {
        const intensity = Math.min(c.count / maxCount, 1);
        const scoreColor =
          c.avgScore >= 80 ? "#C0392B" : c.avgScore >= 65 ? "#D4623A" : "#5C7553";
        return (
          <div
            key={c.category}
            className="px-2.5 py-1.5 rounded-md text-xs font-medium transition-all"
            style={{
              backgroundColor: scoreColor,
              opacity: 0.25 + intensity * 0.75,
              color: "white",
            }}
            title={`${c.category}: ${c.count}건, 평균 ${c.avgScore}점`}
          >
            {c.category}
            <span className="ml-1 opacity-80">{c.count}</span>
          </div>
        );
      })}
    </div>
  );
}
