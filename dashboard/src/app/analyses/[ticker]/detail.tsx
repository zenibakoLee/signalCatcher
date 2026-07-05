"use client";

import { useState } from "react";
import type { CompanyAnalysis } from "@/lib/types";
import { ExpandableText } from "@/components/expandable-text";

interface FiveQuestion {
  question: string;
  answer: "yes" | "partial" | "no" | "unknown";
  evidence: string;
}

interface TimelineEvent {
  date: string;
  event: string;
  significance: string;
}

interface RiskFactor {
  factor: string;
  severity: "high" | "medium" | "low";
}

interface WebArticle {
  title: string;
  url: string;
  date: string;
}

interface KeySignals {
  signals_used: number;
  avg_score: number;
  fake_or_real: { judgment: string; reasoning: string };
  action_note: string;
  top_signals: { title: string; source: string; score: number; url: string }[];
  web_articles?: WebArticle[];
}

function scoreColor(score: number) {
  if (score >= 70) return "bg-ember";
  if (score >= 50) return "bg-sage";
  return "bg-warm-gray";
}

function verdictColor(verdict: string) {
  if (verdict.includes("강한")) return "text-ember";
  if (verdict.includes("관심")) return "text-sage";
  if (verdict.includes("약화")) return "text-warm-gray";
  return "text-red-alert";
}

function answerIcon(answer: string) {
  switch (answer) {
    case "yes":
      return { icon: "+", cls: "text-sage bg-sage/10" };
    case "partial":
      return { icon: "~", cls: "text-ember bg-ember/10" };
    case "no":
      return { icon: "-", cls: "text-red-alert bg-red-alert/10" };
    default:
      return { icon: "?", cls: "text-warm-gray bg-cream-dark" };
  }
}

function severityBadge(severity: string) {
  switch (severity) {
    case "high":
      return "bg-red-alert/10 text-red-alert";
    case "medium":
      return "bg-ember/10 text-ember";
    default:
      return "bg-cream-dark text-warm-gray";
  }
}

function safeJson<T>(raw: string | null | undefined, fallback: T): T {
  if (!raw) return fallback;
  try {
    return JSON.parse(raw);
  } catch {
    return fallback;
  }
}

const DEFAULT_KEY_SIGNALS: KeySignals = {
  signals_used: 0,
  avg_score: 0,
  fake_or_real: { judgment: "", reasoning: "" },
  action_note: "",
  top_signals: [],
};

export function AnalysisDetail({ analyses }: { analyses: CompanyAnalysis[] }) {
  const [selectedIndex, setSelectedIndex] = useState(0);
  const a = analyses[selectedIndex];

  const questions: FiveQuestion[] = safeJson(a.five_questions, []);
  const timeline: TimelineEvent[] = safeJson(a.signal_timeline, []);
  const risks: RiskFactor[] = safeJson(a.risk_factors, []);
  const keySignals: KeySignals = safeJson(
    a.key_signals_json,
    DEFAULT_KEY_SIGNALS
  );

  return (
    <div>
      {/* Date tabs */}
      {analyses.length > 1 && (
        <div className="flex gap-2 mb-4 overflow-x-auto pb-1">
          {analyses.map((an, i) => (
            <button
              key={an.id}
              onClick={() => setSelectedIndex(i)}
              className={`px-3 py-1.5 rounded-full text-xs whitespace-nowrap transition-colors ${
                i === selectedIndex
                  ? "bg-sage text-white"
                  : "bg-cream-dark text-warm-gray hover:bg-light-gray"
              }`}
            >
              {an.generated_at.slice(0, 10)}
            </button>
          ))}
        </div>
      )}

      <article className="bg-white rounded-lg border border-light-gray overflow-hidden">
        {/* Header */}
        <div className="p-5 border-b border-light-gray">
          <div className="flex items-start justify-between gap-4">
            <div className="flex-1">
              <p
                className={`font-serif text-lg font-bold ${verdictColor(a.verdict)}`}
              >
                {a.verdict}
              </p>
              <p className="text-sm text-warm-gray mt-1">{a.verdict_summary}</p>
            </div>
            <div className="text-center shrink-0">
              <div
                className={`w-14 h-14 rounded-full flex items-center justify-center text-white font-mono font-bold text-lg ${scoreColor(a.momentum_score)}`}
              >
                {a.momentum_score}
              </div>
              <div className="text-xs text-warm-gray mt-1">모멘텀</div>
            </div>
          </div>
          <div className="flex gap-3 mt-3 text-xs text-warm-gray">
            <span>
              시그널 {a.signal_count}개 / {a.signal_window_days}일
            </span>
            <span>평균 점수 {keySignals.avg_score}</span>
            <span>{a.generated_at.slice(0, 10)}</span>
          </div>
        </div>

        {/* 5 Questions */}
        {questions.length > 0 && (
          <div className="p-5 border-b border-light-gray">
            <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-3">
              5대 질문 진단
            </h3>
            <div className="space-y-2">
              {questions.map((q, i) => {
                const { icon, cls } = answerIcon(q.answer);
                return (
                  <div key={i} className="flex items-start gap-2">
                    <span
                      className={`shrink-0 w-6 h-6 rounded flex items-center justify-center font-mono text-xs font-bold ${cls}`}
                    >
                      {icon}
                    </span>
                    <div className="flex-1 min-w-0">
                      <ExpandableText lines={2} className="text-sm">
                        {q.evidence}
                      </ExpandableText>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        )}

        {/* Fake vs Real + Action */}
        {(keySignals.fake_or_real?.reasoning || keySignals.action_note) && (
          <div className="p-5 border-b border-light-gray grid grid-cols-1 sm:grid-cols-2 gap-4">
            {keySignals.fake_or_real?.reasoning && (
              <div>
                <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-1">
                  테마 vs 청구서
                  <span
                    className={`ml-2 px-1.5 py-0.5 rounded text-xs ${
                      keySignals.fake_or_real.judgment === "real"
                        ? "bg-sage/10 text-sage"
                        : keySignals.fake_or_real.judgment === "fake"
                          ? "bg-red-alert/10 text-red-alert"
                          : "bg-cream-dark text-warm-gray"
                    }`}
                  >
                    {keySignals.fake_or_real.judgment}
                  </span>
                </h3>
                <p className="text-sm text-warm-gray">
                  {keySignals.fake_or_real.reasoning}
                </p>
              </div>
            )}
            {keySignals.action_note && (
              <div>
                <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-1">
                  관전 포인트
                </h3>
                <p className="text-sm font-medium">{keySignals.action_note}</p>
              </div>
            )}
          </div>
        )}

        {/* Signal Timeline */}
        {timeline.length > 0 && (
          <div className="p-5 border-b border-light-gray">
            <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-3">
              시그널 타임라인
            </h3>
            <div className="space-y-2">
              {timeline.slice(0, 6).map((t, i) => (
                <div key={i} className="flex items-start gap-3">
                  <span className="shrink-0 font-mono text-xs text-warm-gray pt-0.5 w-20">
                    {t.date}
                  </span>
                  <div className="flex-1">
                    <span className="text-sm font-medium">{t.event}</span>
                    <span className="text-xs text-warm-gray ml-2">
                      {t.significance}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Risks */}
        {risks.length > 0 && (
          <div className="p-5 border-b border-light-gray">
            <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-3">
              리스크 요인
            </h3>
            <div className="flex flex-wrap gap-2">
              {risks.map((r, i) => (
                <span
                  key={i}
                  className={`text-xs px-2 py-1 rounded-full ${severityBadge(r.severity)}`}
                >
                  {r.factor}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* Top Signals */}
        {keySignals.top_signals?.length > 0 && (
          <div className="p-5 border-b border-light-gray">
            <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-3">
              근거 시그널 상위
            </h3>
            <div className="space-y-1">
              {keySignals.top_signals.slice(0, 5).map((s, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <span className="font-mono text-xs text-sage font-bold w-6">
                    {s.score}
                  </span>
                  <span className="text-xs px-1.5 py-0.5 rounded bg-cream-dark text-warm-gray">
                    {s.source}
                  </span>
                  {s.url ? (
                    <a
                      href={s.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="truncate hover:text-sage transition-colors"
                    >
                      {s.title}
                    </a>
                  ) : (
                    <span className="truncate">{s.title}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* Web Articles */}
        {keySignals.web_articles && keySignals.web_articles.length > 0 && (
          <div className="p-5">
            <h3 className="text-xs font-bold text-warm-gray uppercase tracking-wide mb-3">
              참고 기사 (웹 검색)
            </h3>
            <div className="space-y-1">
              {keySignals.web_articles.slice(0, 6).map((art, i) => (
                <div key={i} className="flex items-center gap-2 text-sm">
                  <span className="text-xs text-warm-gray/60 shrink-0 w-14">
                    {art.date?.slice(5, 16) || ""}
                  </span>
                  {art.url ? (
                    <a
                      href={art.url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="truncate hover:text-sage transition-colors"
                    >
                      {art.title}
                    </a>
                  ) : (
                    <span className="truncate">{art.title}</span>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}
      </article>
    </div>
  );
}
