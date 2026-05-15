"use client";

interface CoNode {
  keyword: string;
  totalCoCount: number;
}

interface CoEdge {
  source: string;
  target: string;
  weight: number;
}

export function NetworkGraph({
  nodes,
  edges,
}: {
  nodes: CoNode[];
  edges: CoEdge[];
}) {
  if (nodes.length === 0) {
    return <p className="text-warm-gray text-sm">동시 출현 데이터 없음</p>;
  }

  const width = 800;
  const height = 500;
  const cx = width / 2;
  const cy = height / 2;

  const maxWeight = Math.max(...edges.map((e) => e.weight), 1);
  const maxNodeSize = Math.max(...nodes.map((n) => n.totalCoCount), 1);

  const nodePositions: Record<string, { x: number; y: number }> = {};
  const angleStep = (2 * Math.PI) / nodes.length;
  const radius = Math.min(width, height) * 0.35;

  nodes.forEach((node, i) => {
    const angle = angleStep * i - Math.PI / 2;
    nodePositions[node.keyword] = {
      x: cx + radius * Math.cos(angle),
      y: cy + radius * Math.sin(angle),
    };
  });

  return (
    <div className="bg-white rounded-lg border border-light-gray p-4">
      <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto">
        {edges.map((edge, i) => {
          const from = nodePositions[edge.source];
          const to = nodePositions[edge.target];
          if (!from || !to) return null;
          const opacity = 0.15 + (edge.weight / maxWeight) * 0.7;
          const strokeWidth = 1 + (edge.weight / maxWeight) * 4;
          return (
            <line
              key={i}
              x1={from.x}
              y1={from.y}
              x2={to.x}
              y2={to.y}
              stroke="#5C7553"
              strokeWidth={strokeWidth}
              opacity={opacity}
            />
          );
        })}

        {nodes.map((node) => {
          const pos = nodePositions[node.keyword];
          const r = 8 + (node.totalCoCount / maxNodeSize) * 20;
          return (
            <g key={node.keyword}>
              <circle
                cx={pos.x}
                cy={pos.y}
                r={r}
                fill="#5C7553"
                opacity={0.8}
              />
              <text
                x={pos.x}
                y={pos.y + r + 14}
                textAnchor="middle"
                fontSize={11}
                fontWeight="600"
                className="fill-charcoal"
              >
                {node.keyword}
              </text>
              <text
                x={pos.x}
                y={pos.y + 4}
                textAnchor="middle"
                fontSize={9}
                fontWeight="bold"
                className="fill-white"
              >
                {node.totalCoCount}
              </text>
            </g>
          );
        })}
      </svg>

      <div className="mt-3 text-xs text-warm-gray text-center">
        노드 크기 = 동시 출현 횟수 합계 · 선 굵기 = 쌍 빈도
      </div>
    </div>
  );
}
