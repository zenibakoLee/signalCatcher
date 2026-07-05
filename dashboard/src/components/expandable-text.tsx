"use client";

import { useState, useRef, useEffect } from "react";

export function ExpandableText({
  children,
  lines = 2,
  className = "",
}: {
  children: React.ReactNode;
  lines?: number;
  className?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [clamped, setClamped] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    setClamped(el.scrollHeight > el.clientHeight + 1);
  }, [children]);

  const clampClass = !expanded
    ? lines === 1
      ? "line-clamp-1"
      : lines === 3
        ? "line-clamp-3"
        : "line-clamp-2"
    : "";

  return (
    <div
      ref={ref}
      className={`${clampClass} ${className} ${clamped && !expanded ? "cursor-pointer active:opacity-70" : ""}`}
      onClick={
        clamped
          ? () => setExpanded((v) => !v)
          : undefined
      }
    >
      {children}
    </div>
  );
}
