import assert from "node:assert/strict";
import test from "node:test";

import { getDailyFreshness, startDailyFreshnessMonitor } from "./freshness.ts";

const NOW = new Date("2026-09-13T12:00:00.000Z");

test("marks a daily run fresh at exactly 26 hours", () => {
  const result = getDailyFreshness("2026-09-12T10:00:00.000Z", NOW);

  assert.equal(result.isStale, false);
  assert.equal(result.ageHours, 26);
});

test("marks a daily run stale after 26 hours", () => {
  const result = getDailyFreshness("2026-09-12T09:59:59.999Z", NOW);

  assert.equal(result.isStale, true);
  assert.ok(result.ageHours > 26);
});

test("monitor transitions an open dashboard to stale after the deadline", () => {
  let currentTime = new Date("2026-09-13T11:00:00.000Z");
  let tick: (() => void) | undefined;
  let clearedTimer: unknown;
  const updates: boolean[] = [];

  const stop = startDailyFreshnessMonitor(
    "2026-09-12T10:00:00.000Z",
    (freshness) => updates.push(freshness.isStale),
    {
      now: () => currentTime,
      setInterval: (callback, intervalMs) => {
        assert.equal(intervalMs, 60_000);
        tick = callback;
        return "timer";
      },
      clearInterval: (timer) => {
        clearedTimer = timer;
      },
    },
  );

  assert.deepEqual(updates, [false]);
  currentTime = new Date("2026-09-13T12:00:00.001Z");
  assert.ok(tick);
  tick();
  assert.deepEqual(updates, [false, true]);

  stop();
  assert.equal(clearedTimer, "timer");
});

test("interprets legacy offsetless pipeline timestamps as KST", () => {
  const previousTz = process.env.TZ;
  process.env.TZ = "UTC";
  try {
    const result = getDailyFreshness(
      "2026-09-13T07:00:00",
      new Date("2026-09-13T00:00:00.000Z"),
    );

    assert.equal(result.ageHours, 2);
    assert.equal(result.isStale, false);
  } finally {
    process.env.TZ = previousTz;
  }
});

test("marks materially future completion timestamps invalid and stale", () => {
  const result = getDailyFreshness("2026-09-13T12:06:00.000Z", NOW);

  assert.deepEqual(result, {
    isStale: true,
    ageHours: null,
    lastSuccessfulRun: "2026-09-13T12:06:00.000Z",
  });
});

test("marks missing successful daily runs stale", () => {
  const result = getDailyFreshness(null, NOW);

  assert.deepEqual(result, {
    isStale: true,
    ageHours: null,
    lastSuccessfulRun: null,
  });
});

test("marks invalid completion timestamps stale", () => {
  const result = getDailyFreshness("not-a-timestamp", NOW);

  assert.deepEqual(result, {
    isStale: true,
    ageHours: null,
    lastSuccessfulRun: "not-a-timestamp",
  });
});
