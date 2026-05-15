"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { useState, useMemo } from "react";

const DAYS_KR = ["일", "월", "화", "수", "목", "금", "토"];
const MONTHS_KR = [
  "1월", "2월", "3월", "4월", "5월", "6월",
  "7월", "8월", "9월", "10월", "11월", "12월",
];

function toDateStr(d: Date) {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const day = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${day}`;
}

export function DatePicker({
  currentDate,
  availableDates,
}: {
  currentDate: string;
  availableDates: string[];
}) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const [open, setOpen] = useState(false);

  const current = new Date(currentDate + "T00:00:00");
  const [viewYear, setViewYear] = useState(current.getFullYear());
  const [viewMonth, setViewMonth] = useState(current.getMonth());

  const dateSet = useMemo(() => new Set(availableDates), [availableDates]);

  const sorted = useMemo(
    () => [...availableDates].sort(),
    [availableDates]
  );
  const currentIdx = sorted.indexOf(currentDate);

  function navigate(date: string) {
    const params = new URLSearchParams(searchParams.toString());
    if (date === sorted[sorted.length - 1]) {
      params.delete("date");
    } else {
      params.set("date", date);
    }
    router.push(`/?${params.toString()}`);
    setOpen(false);
  }

  const firstDay = new Date(viewYear, viewMonth, 1).getDay();
  const daysInMonth = new Date(viewYear, viewMonth + 1, 0).getDate();

  function prevMonth() {
    if (viewMonth === 0) {
      setViewYear(viewYear - 1);
      setViewMonth(11);
    } else {
      setViewMonth(viewMonth - 1);
    }
  }

  function nextMonth() {
    if (viewMonth === 11) {
      setViewYear(viewYear + 1);
      setViewMonth(0);
    } else {
      setViewMonth(viewMonth + 1);
    }
  }

  return (
    <div className="flex items-center gap-2">
      <button
        onClick={() => currentIdx > 0 && navigate(sorted[currentIdx - 1])}
        disabled={currentIdx <= 0}
        className="p-1.5 rounded-full hover:bg-cream-dark disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        aria-label="이전 날짜"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      <button
        onClick={() => setOpen(!open)}
        className="px-3 py-1.5 rounded-full text-sm font-medium border border-light-gray bg-white hover:bg-cream-dark transition-colors flex items-center gap-1.5"
      >
        <svg width="14" height="14" viewBox="0 0 14 14" fill="none">
          <rect x="1" y="2" width="12" height="11" rx="1.5" stroke="currentColor" strokeWidth="1.2"/>
          <path d="M1 5.5H13" stroke="currentColor" strokeWidth="1.2"/>
          <path d="M4 1V3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
          <path d="M10 1V3" stroke="currentColor" strokeWidth="1.2" strokeLinecap="round"/>
        </svg>
        {currentDate}
      </button>

      <button
        onClick={() => currentIdx < sorted.length - 1 && navigate(sorted[currentIdx + 1])}
        disabled={currentIdx >= sorted.length - 1}
        className="p-1.5 rounded-full hover:bg-cream-dark disabled:opacity-30 disabled:cursor-not-allowed transition-colors"
        aria-label="다음 날짜"
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <path d="M6 4L10 8L6 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
        </svg>
      </button>

      {currentIdx < sorted.length - 1 && (
        <button
          onClick={() => navigate(sorted[sorted.length - 1])}
          className="px-2.5 py-1 rounded-full text-xs bg-sage text-white hover:bg-sage-light transition-colors"
        >
          최신
        </button>
      )}

      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute top-full mt-2 z-50 bg-white border border-light-gray rounded-xl shadow-lg p-4 w-72">
            <div className="flex items-center justify-between mb-3">
              <button onClick={prevMonth} className="p-1 rounded hover:bg-cream-dark transition-colors">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path d="M10 12L6 8L10 4" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
              <span className="text-sm font-bold">
                {viewYear}년 {MONTHS_KR[viewMonth]}
              </span>
              <button onClick={nextMonth} className="p-1 rounded hover:bg-cream-dark transition-colors">
                <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
                  <path d="M6 4L10 8L6 12" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            </div>

            <div className="grid grid-cols-7 gap-0.5 text-center">
              {DAYS_KR.map((d) => (
                <div key={d} className="text-xs text-warm-gray py-1">{d}</div>
              ))}
              {Array.from({ length: firstDay }).map((_, i) => (
                <div key={`e-${i}`} />
              ))}
              {Array.from({ length: daysInMonth }).map((_, i) => {
                const day = i + 1;
                const dateStr = toDateStr(new Date(viewYear, viewMonth, day));
                const hasData = dateSet.has(dateStr);
                const isSelected = dateStr === currentDate;
                return (
                  <button
                    key={day}
                    onClick={() => hasData && navigate(dateStr)}
                    disabled={!hasData}
                    className={`text-xs py-1.5 rounded-lg transition-colors ${
                      isSelected
                        ? "bg-sage text-white font-bold"
                        : hasData
                          ? "hover:bg-cream-dark font-medium text-charcoal"
                          : "text-light-gray cursor-not-allowed"
                    }`}
                  >
                    {day}
                    {hasData && !isSelected && (
                      <div className="w-1 h-1 bg-sage rounded-full mx-auto mt-0.5" />
                    )}
                  </button>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}
