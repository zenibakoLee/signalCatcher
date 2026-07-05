"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";

export function AnalysisSearch() {
  const [ticker, setTicker] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const router = useRouter();

  async function handleGenerate(e: React.FormEvent) {
    e.preventDefault();
    const value = ticker.trim().toUpperCase();
    if (!value || loading) return;

    setLoading(true);
    setError("");

    try {
      const resp = await fetch("/api/analyses/generate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ticker: value }),
      });
      const data = await resp.json();

      if (data.error) {
        setError("분석 생성에 실패했습니다. 잠시 후 다시 시도해주세요.");
      } else {
        router.push(`/analyses/${value}`);
        router.refresh();
      }
    } catch {
      setError("서버 오류가 발생했습니다.");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div>
      <form onSubmit={handleGenerate} className="flex gap-2">
        <input
          type="text"
          value={ticker}
          onChange={(e) => {
            setTicker(e.target.value);
            setError("");
          }}
          placeholder="종목 코드 입력 (예: NVDA, TSLA, AMZN)"
          className="flex-1 px-4 py-2.5 rounded-lg border border-light-gray bg-white text-sm focus:outline-none focus:border-sage transition-colors font-mono placeholder:font-sans placeholder:text-warm-gray/50"
          maxLength={10}
          disabled={loading}
        />
        <button
          type="submit"
          disabled={!ticker.trim() || loading}
          className="px-5 py-2.5 rounded-lg bg-sage text-white text-sm font-medium disabled:opacity-40 hover:bg-sage-light transition-colors shrink-0"
        >
          {loading ? (
            <span className="flex items-center gap-2">
              <span className="w-4 h-4 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              분석 중...
            </span>
          ) : (
            "분석 생성"
          )}
        </button>
      </form>
      {loading && (
        <p className="text-xs text-warm-gray mt-2">
          AI가 시그널과 뉴스를 분석 중입니다. 30초~1분 정도 소요됩니다.
        </p>
      )}
      {error && <p className="text-xs text-red-alert mt-2">{error}</p>}
    </div>
  );
}
