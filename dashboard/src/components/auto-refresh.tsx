"use client";

import { useRouter } from "next/navigation";
import { useEffect, useRef } from "react";

export function AutoRefresh({ intervalMs = 3_000 }: { intervalMs?: number }) {
  const router = useRouter();
  const lastMtime = useRef<number>(0);

  useEffect(() => {
    const id = setInterval(async () => {
      try {
        const res = await fetch("/api/db-version");
        const { mtime } = await res.json();
        if (lastMtime.current !== 0 && mtime !== lastMtime.current) {
          router.refresh();
        }
        lastMtime.current = mtime;
      } catch {
        // ignore fetch errors
      }
    }, intervalMs);
    return () => clearInterval(id);
  }, [router, intervalMs]);

  return null;
}
