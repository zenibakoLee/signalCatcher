"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useCallback } from "react";

export function SignalFilters({
  sources,
  categories,
  current,
}: {
  sources: string[];
  categories: string[];
  current: { source?: string; category?: string; minScore?: string };
}) {
  const router = useRouter();
  const searchParams = useSearchParams();

  const update = useCallback(
    (key: string, value: string) => {
      const params = new URLSearchParams(searchParams.toString());
      if (value) {
        params.set(key, value);
      } else {
        params.delete(key);
      }
      router.push(`/signals?${params.toString()}`);
    },
    [router, searchParams]
  );

  return (
    <div className="flex flex-wrap gap-3">
      <select
        value={current.source || ""}
        onChange={(e) => update("source", e.target.value)}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">모든 소스</option>
        {sources.map((s) => (
          <option key={s} value={s}>{s}</option>
        ))}
      </select>

      <select
        value={current.category || ""}
        onChange={(e) => update("category", e.target.value)}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">모든 카테고리</option>
        {categories.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>

      <select
        value={current.minScore || ""}
        onChange={(e) => update("minScore", e.target.value)}
        className="text-sm border border-light-gray rounded-full px-3 py-1.5 bg-white text-charcoal"
      >
        <option value="">최소 점수 없음</option>
        <option value="50">50+</option>
        <option value="70">70+</option>
        <option value="85">85+</option>
      </select>
    </div>
  );
}
