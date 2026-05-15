"use client";

import { useRouter, useSearchParams } from "next/navigation";

const RANGES = [
  { value: "7", label: "7일" },
  { value: "30", label: "30일" },
  { value: "90", label: "90일" },
  { value: "all", label: "전체" },
];

export function TimeRange({ current }: { current: string }) {
  const router = useRouter();
  const searchParams = useSearchParams();

  function select(value: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (value === "30") {
      params.delete("days");
    } else {
      params.set("days", value);
    }
    router.push(`/trends?${params.toString()}`);
  }

  return (
    <div className="flex gap-1">
      {RANGES.map(({ value, label }) => (
        <button
          key={value}
          onClick={() => select(value)}
          className={`px-3 py-1.5 rounded-full text-sm transition-colors ${
            current === value
              ? "bg-sage text-white"
              : "text-warm-gray hover:bg-cream-dark border border-light-gray"
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}
