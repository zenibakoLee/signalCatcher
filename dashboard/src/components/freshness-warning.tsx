"use client";

import { useEffect, useState } from "react";

import {
  startDailyFreshnessMonitor,
  type DailyFreshness,
} from "@/lib/freshness";

export function FreshnessWarning({ freshness }: { freshness: DailyFreshness }) {
  const [current, setCurrent] = useState(freshness);

  useEffect(
    () => startDailyFreshnessMonitor(freshness.lastSuccessfulRun, setCurrent),
    [freshness.lastSuccessfulRun],
  );

  if (!current.isStale) return null;

  let detail = "정상 완료된 daily 실행 기록이 없습니다.";
  if (current.lastSuccessfulRun && current.ageHours !== null) {
    detail = `마지막 정상 완료: ${current.lastSuccessfulRun.replace("T", " ")} (${Math.floor(current.ageHours)}시간 전)`;
  } else if (current.lastSuccessfulRun) {
    detail = `마지막 완료 시각이 유효하지 않습니다: ${current.lastSuccessfulRun}`;
  }

  return (
    <section
      role="alert"
      className="rounded-lg border border-red-alert/40 bg-red-alert/10 p-4 text-red-alert"
    >
      <h2 className="font-bold">⚠ 데이터 업데이트 지연</h2>
      <p className="mt-1 text-sm">
        {detail} 26시간 freshness 기준을 초과했으므로 현재 순위를 최신 투자 결과로 사용하지 마세요.
      </p>
    </section>
  );
}
