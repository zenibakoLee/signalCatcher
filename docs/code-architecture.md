# Code Architecture

## 디렉토리 구조

```
signalCatcher/
├── config/              # YAML 설정 (keywords, sources, conferences, scoring_prompt)
│   │                    # sources.yaml: RSS 12개, YouTube 12채널+4검색쿼리, arXiv 5카테고리
├── pipeline/            # Python 백엔드
│   ├── main.py          # Click CLI: daily|backfill|event|score-all|translate-titles|analyze
│   ├── db.py            # SQLite 연결, 스키마(9 테이블), 마이그레이션
│   ├── models.py        # Pydantic: RawItem, ScoredItem, TrendAlert
│   ├── collectors/      # 소스별 수집기 (BaseCollector ABC): HN, RSS, arXiv, GitHub, YouTube (+ApeWisdom 버즈)
│   ├── processing/      # dedup, keyword_counter, scorer, trend_detector, transcript
│   ├── generators/      # daily_digest, conference_briefing, keyword_suggestions, company_analysis
│   ├── delivery/        # discord_webhook (다이제스트, 트렌드, 브리핑, 기업분석, 에러)
│   ├── scripts/         # generate_analysis.py (대시보드 API에서 호출)
│   └── utils/           # rate_limiter, retry, logging_config
├── dashboard/           # Next.js 16 + Tailwind (read-only SQLite)
│   └── src/
│       ├── app/         # 7개 페이지 + API routes (analyses, analyses/generate)
│       ├── components/  # nav, date-picker, auto-refresh, network-graph, charts, expandable-text
│       └── lib/         # db.ts (better-sqlite3), types.ts
├── launchd/             # macOS 서비스 (4개 plist + install.sh)
├── scripts/             # run-daily.sh, start-dashboard.sh, start-tunnel.sh
└── data/                # gitignored: signalcatcher.db, logs/
```

## 파이프라인 흐름 (daily)

```
CLI(click) → collect_all(asyncio, 5 collectors 순차: HN→RSS→arXiv→GitHub→YouTube)
           → deduplicate_and_store(INSERT OR IGNORE)
           → enrich_youtube_transcripts(yt-dlp 자막 추출)
           → auto_manage_keywords(발견/스파이크/은퇴)
           → count_keywords_for_items(regex word-boundary)
           → detect_trends(z-score + 장기 가속)
           → score_items(Claude Haiku, 배치 10, title_ko + related_tickers)
           → generate_digest(Claude Haiku)
           → deliver_digest(Discord webhook)
           → deliver_acceleration_alerts(장기 가속 알림)
           → run_company_analyses(Claude Sonnet, 모멘텀 상위 종목)
           → deliver_company_analyses(Discord webhook)
```

### 기업 분석 흐름 (analyze)

```
find_momentum_candidates(related_tickers 집계 + social_buzz)
→ _fetch_news(Google News RSS)
→ generate_analysis(Claude Sonnet, 5대 질문 프레임워크)
→ company_analyses 테이블 저장
→ deliver_company_analyses(Discord webhook)
```

## 핵심 패턴

**수집기 격리**: 각 collector는 try/except로 감싸짐. 하나 실패해도 나머지 진행.

**Rate Limiting**: 소스별 토큰 버킷 (`RateLimiter`). arXiv 0.1/s (키워드 8개씩 OR 배칭으로 쿼리 수 ~85% 절감), HN 2/s, GitHub 0.5/s, YouTube 1/s, RSS 2/s.

**재시도**: `with_retry()` — 3회, 지수 백오프, 429는 15s×attempt. NON_RETRYABLE: {401,403,404,422}.

**멱등성**: `UNIQUE` 제약 + `ON CONFLICT DO UPDATE`. 같은 날 2회 실행 안전.

**backfill의 date 처리**: `use_item_dates=True`로 각 아이템의 `published_at` 기준 날짜별 그룹핑. 일반 daily는 `target_date=today`.

**LLM 통합**: 모든 LLM 호출은 JSON 응답 요청 → markdown fence 제거 → `json.loads` → fallback. 모델: Haiku(스코어링/다이제스트/트렌드/키워드 발견), Sonnet(컨퍼런스/기업분석).

**YouTube 자막 보강**: `yt-dlp`로 YouTube 영상의 자동 생성 자막(en/ko)을 추출하여 `content_snippet`에 추가. 스코어링 정확도 향상. 기존 snippet이 600자 미만인 항목만 대상.

**YouTube 점수 보정**: `search` API로 수집된 YouTube 항목은 25% 감점 (채널 구독 기반 수집보다 신뢰도 낮음).

**관련종목 매핑**: 스코어링 시 LLM이 각 시그널에 관련 종목(티커) 1~3개를 함께 반환. `scored_items.related_tickers`에 JSON 배열로 저장.

## 대시보드

- Server Components (기본) — DB 직접 조회, 클라이언트 번들 최소화
- Client Components — 필터(`useSearchParams`), 캘린더 날짜 선택, 네트워크 그래프(SVG), 차트(`charts.tsx`), 텍스트 확장(`expandable-text.tsx`), 자동 새로고침
- 시각 컴포넌트 (`charts.tsx`): `ScoreDistribution`(점수 분포 바), `SourceBreakdown`(소스별 스택바+범례), `Sparkline`(SVG 미니 추이 차트), `StatCard`(통계 카드), `CategoryHeatmap`(카테고리 히트맵)
- `ExpandableText`: 줄임 텍스트 클릭 시 전체 표시 (line-clamp + 자동 감지)
- `better-sqlite3` read-only 연결, `../data/signalcatcher.db` 참조
- Next.js 16: `params`, `searchParams`는 `Promise<{}>` — 반드시 `await`
- 자동 새로고침: DB 파일 mtime 폴링 (3초) → `router.refresh()`로 Server Components 재실행
- 한국어 제목 우선 표시 (`title_ko || title`), 영어 원문은 보조 텍스트
- 관련종목 표시: `related_tickers` JSON 파싱 → 관련종목 배지 표시
- 기업 분석 페이지: 모멘텀 카드 그리드 + 종목별 상세 (5대 질문, 타임라인, 리스크, 근거 시그널, 웹 기사) + 온디맨드 분석 API (`/api/analyses/generate` → `pipeline/scripts/generate_analysis.py`)
- Cloudflare Quick Tunnel로 외부 접속 (URL 변경 시 Discord 자동 전송)

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
