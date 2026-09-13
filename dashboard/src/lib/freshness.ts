export const DAILY_STALE_AFTER_HOURS = 26;
export const FUTURE_TIMESTAMP_TOLERANCE_MINUTES = 5;

const EXPLICIT_TIMEZONE_SUFFIX = /(?:Z|[+-]\d{2}:\d{2})$/i;

export function parsePipelineTimestamp(value: string): Date {
  // Pipeline timestamps written before UTC normalization used the host's KST wall clock.
  return new Date(EXPLICIT_TIMEZONE_SUFFIX.test(value) ? value : `${value}+09:00`);
}

export interface DailyFreshness {
  isStale: boolean;
  ageHours: number | null;
  lastSuccessfulRun: string | null;
}

export interface FreshnessMonitorScheduler {
  now: () => Date;
  setInterval: (callback: () => void, intervalMs: number) => unknown;
  clearInterval: (timer: unknown) => void;
}

const DEFAULT_MONITOR_SCHEDULER: FreshnessMonitorScheduler = {
  now: () => new Date(),
  setInterval: (callback, intervalMs) => globalThis.setInterval(callback, intervalMs),
  clearInterval: (timer) =>
    globalThis.clearInterval(timer as ReturnType<typeof globalThis.setInterval>),
};

export function getDailyFreshness(
  lastSuccessfulRun: string | null,
  now: Date = new Date(),
): DailyFreshness {
  if (!lastSuccessfulRun) {
    return { isStale: true, ageHours: null, lastSuccessfulRun: null };
  }

  const completedAt = parsePipelineTimestamp(lastSuccessfulRun);
  if (Number.isNaN(completedAt.getTime())) {
    return { isStale: true, ageHours: null, lastSuccessfulRun };
  }

  const ageHours = (now.getTime() - completedAt.getTime()) / 3_600_000;
  if (ageHours < -FUTURE_TIMESTAMP_TOLERANCE_MINUTES / 60) {
    return { isStale: true, ageHours: null, lastSuccessfulRun };
  }

  return {
    isStale: ageHours > DAILY_STALE_AFTER_HOURS,
    ageHours,
    lastSuccessfulRun,
  };
}

export function startDailyFreshnessMonitor(
  lastSuccessfulRun: string | null,
  onChange: (freshness: DailyFreshness) => void,
  scheduler: FreshnessMonitorScheduler = DEFAULT_MONITOR_SCHEDULER,
): () => void {
  const update = () => {
    onChange(getDailyFreshness(lastSuccessfulRun, scheduler.now()));
  };

  update();
  const timer = scheduler.setInterval(update, 60_000);
  return () => scheduler.clearInterval(timer);
}
