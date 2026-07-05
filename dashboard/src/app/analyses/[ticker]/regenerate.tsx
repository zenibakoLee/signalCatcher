"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export function RegenerateButton({ ticker }: { ticker: string }) {
  const [loading, setLoading] = useState(false);
  const router = useRouter();

  async function handleRegenerate() {
    if (loading) return;
    setLoading(true);

    try {
      const resp = await fetch("/api/analyses/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker }),
      });
      if (resp.ok) {
        router.refresh();
      }
    } catch {
      // silently fail
    } finally {
      setLoading(false);
    }
  }

  return (
    <button
      onClick={handleRegenerate}
      disabled={loading}
      className="px-4 py-2 rounded-lg border border-light-gray text-sm text-warm-gray hover:border-sage hover:text-sage transition-colors disabled:opacity-40"
    >
      {loading ? (
        <span className="flex items-center gap-2">
          <span className="w-3.5 h-3.5 border-2 border-warm-gray/30 border-t-warm-gray rounded-full animate-spin" />
          분석 중...
        </span>
      ) : (
        "재분석"
      )}
    </button>
  );
}
