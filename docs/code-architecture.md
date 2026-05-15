# Code Architecture

## 디렉토리 구조

```
signalCatcher/
├── config/              # YAML 설정 (keywords, sources, conferences, scoring_prompt)
├── pipeline/            # Python 백엔드
│   ├── main.py          # Click CLI: daily|backfill|weekly|event
│   ├── db.py            # SQLite 연결, 스키마, 헬퍼
│   ├── models.py        # Pydantic: RawItem, ScoredItem, TrendAlert
│   ├── collectors/      # 소스별 수집기 (BaseCollector ABC)
│   ├── processing/      # dedup, keyword_counter, scorer, trend_detector
│   ├── generators/      # daily_digest, conference_briefing, keyword_suggestions
│   ├── delivery/        # discord_webhook (다이제스트, 트렌드, 브리핑, 에러)
│   └── utils/           # rate_limiter, retry, logging_config
├── dashboard/           # Next.js 16 + Tailwind (read-only SQLite)
│   └── src/
│       ├── app/         # 5개 페이지 + 5개 API route
│       ├── components/  # nav (pill 네비게이션)
│       └── lib/         # db.ts (better-sqlite3), types.ts
├── launchd/             # macOS 스케줄 (3개 plist + install.sh)
└── data/                # gitignored: signalcatcher.db, logs/
```

## 파이프라인 흐름 (daily)

```
CLI(click) → collect_all(asyncio, 5 collectors 순차)
           → deduplicate_and_store(INSERT OR IGNORE)
           → count_keywords_for_items(regex word-boundary)
           → detect_trends(z-score)
           → score_items(Claude Haiku, 배치 20)
           → generate_digest(Claude Haiku)
           → deliver_digest(Discord webhook)
```

## 핵심 패턴

**수집기 격리**: 각 collector는 try/except로 감싸짐. 하나 실패해도 나머지 진행.

**Rate Limiting**: 소스별 토큰 버킷 (`RateLimiter`). arXiv 0.1/s, HN 2/s, GitHub 0.5/s.

**재시도**: `with_retry()` — 3회, 지수 백오프, 429는 15s×attempt. NON_RETRYABLE: {401,403,404,422}.

**멱등성**: `UNIQUE` 제약 + `ON CONFLICT DO UPDATE`. 같은 날 2회 실행 안전.

**backfill의 date 처리**: `use_item_dates=True`로 각 아이템의 `published_at` 기준 날짜별 그룹핑. 일반 daily는 `target_date=today`.

**LLM 통합**: 모든 LLM 호출은 JSON 응답 요청 → markdown fence 제거 → `json.loads` → fallback. 모델: Haiku(스코어링/다이제스트/트렌드), Sonnet(컨퍼런스).

## 대시보드

- Server Components (기본) — DB 직접 조회, 클라이언트 번들 최소화
- Client Components — 필터(`useSearchParams`), 차트(SVG 직접 렌더)
- `better-sqlite3` read-only 연결, `../data/signalcatcher.db` 참조
- Next.js 16: `params`, `searchParams`는 `Promise<{}>` — 반드시 `await`

## 디자인 토큰

| 용도 | 색상 | 코드 |
|------|------|------|
| 배경 | cream | #FAF7F2 |
| 주요 UI | sage green | #5C7553 |
| 트렌드 알림 | ember orange | #D4623A |
| 컨퍼런스 | deep blue | #1E3A5F |
| 키워드 제안 | lavender | #7B68AE |
| 긴급 | red alert | #C0392B |

헤드라인: Noto Serif KR. 본문: Geist Sans. 네비게이션: pill(rounded-full) 버튼.
