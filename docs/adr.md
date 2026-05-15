# Architecture Decision Records

## ADR-001: SQLite + WAL (클라우드 DB 대신)

**결정**: SQLite WAL 모드, 단일 파일 DB.

**맥락**: 사용자 1인, 로컬 Mac 전용. 쓰기는 파이프라인만, 대시보드는 read-only.

**대안 검토**: PostgreSQL(과잉), DuckDB(분석 특화이나 concurrent write 미성숙).

**결과**: 백업 = 파일 복사. 배포 = 불필요. 대시보드는 `better-sqlite3` read-only 연결로 WAL 충돌 없음.

---

## ADR-002: scored_items 분리 (raw_items 내장 대신)

**결정**: 스코어링 결과를 별도 테이블로 분리, `raw_item_id UNIQUE FK`.

**맥락**: 프롬프트 개선 시 재스코어링 필요. 원본 데이터 보존이 투자 판단 검증의 핵심.

**결과**: `scored_items` DROP 후 재생성해도 `raw_items` 무영향. fallback score=50으로 스코어링 실패 격리.

---

## ADR-003: z-score 트렌드 감지 (ML 모델 대신)

**결정**: 30일 이동평균 + 표준편차 기반 z-score. notable(>2.0), urgent(>3.0).

**맥락**: 단일 사용자 시스템에 ML 파이프라인은 과잉. z-score는 해석 가능하고, 임계값 조정이 직관적.

**보호장치**:
- `MIN_HISTORY_DAYS=7`: 콜드스타트 false positive 방지
- `std_dev == 0`이고 `today_count > 0`이면 `z_score = 3.0` (신규 키워드 첫 출현)

**대안**: Prophet(과잉), 단순 이동평균(가속도 감지 불가).

---

## ADR-004: Claude Haiku 스코어링 (Sonnet/Opus 대신)

**결정**: 일일 스코어링·다이제스트·트렌드 해석은 Haiku. 컨퍼런스 브리핑만 Sonnet.

**맥락**: 일일 50~100개 아이템 × 매일. Haiku로 비용 최소화. 컨퍼런스는 빈도 낮고 투자 판단 직결이라 Sonnet.

**배치 전략**: 20개씩 묶어 단일 API 호출. JSON 응답 파싱 실패 시 개별 fallback(score=50).

---

## ADR-005: keyword_daily_aggregates 비정규화

**결정**: `keyword_mentions`(소스별) 외에 `keyword_daily_aggregates`(소스 통합) 유지.

**맥락**: 트렌드 쿼리가 매일 30일치 집계 필요. JOIN + GROUP BY 반복 대신 비정규화로 O(1) 조회.

**트레이드오프**: 저장 중복 발생하나, 데이터 규모(수천 행/일)에서 무시 가능.

---

## ADR-006: 수집기 격리 패턴

**결정**: 각 collector를 `try/except`로 독립 실행. 하나 실패해도 나머지 진행.

**맥락**: arXiv 10초 rate limit으로 타임아웃 빈발, GitHub 403 간헐 발생. 전체 파이프라인이 하나의 소스 장애로 중단되면 투자 신호를 놓침.

**재시도**: `with_retry()` — 3회 지수 백오프. 429는 `15s × attempt`. NON_RETRYABLE: `{401, 403, 404, 422}`.

---

## ADR-007: Discord 단일 채널 (Slack/이메일 대신)

**결정**: Discord webhook으로 모든 알림 전송. 색상 코드로 유형 구분.

**색상 체계**: sage green(다이제스트), ember orange(트렌드), deep blue(컨퍼런스), lavender(키워드 제안), red(에러).

**맥락**: 1인 사용자, 기존 Discord 활용 중. Slack은 유료 히스토리 제한, 이메일은 실시간성 부족.

---

## ADR-008: arXiv 카테고리 필터링

**결정**: `company`, `infrastructure` 카테고리 키워드는 arXiv 검색에서 제외.

**맥락**: arXiv는 학술 논문 DB. "NVIDIA", "AWS" 같은 기업명 검색은 노이즈만 생성. 10초 rate limit 하에서 무의미한 쿼리 20+개 절약 (200초+).

**구현**: `keyword_categories` dict를 모든 collector에 전달, arXiv만 `SKIP_CATEGORIES` 필터 적용.

---

## ADR-009: YouTube playlistItems (Search API 대신)

**결정**: YouTube Data API의 `playlistItems` 엔드포인트 사용 (1유닛/호출).

**맥락**: `search` 엔드포인트는 100유닛/호출. 일일 할당량 10,000유닛에서 검색만으로 소진 위험.

**트레이드오프**: 채널 단위 수집만 가능 (키워드 검색 불가). `sources.yaml`에 관심 채널 등록으로 보완.

---

## ADR-010: Next.js Server Components 기본 (CSR 대신)

**결정**: 대시보드 페이지는 Server Components 기본. 필터·차트만 Client Component.

**맥락**: read-only DB 직접 조회로 API 레이어 최소화. 클라이언트 번들 최소화. 필터(`useSearchParams`)와 SVG 차트만 `'use client'`.

---

## ADR-011: Backfill 전략 (HN + GitHub만)

**결정**: backfill은 HN(Algolia 히스토리)과 GitHub(날짜 쿼리)만 지원.

**맥락**:
- arXiv: 10초 rate limit, 30일 backfill에 수시간 소요 → 비현실적
- RSS: 피드 자체가 최근 항목만 보유
- YouTube: playlistItems로 최근 영상만 접근 가능

**날짜 처리**: `use_item_dates=True`로 각 아이템의 `published_at` 기준 날짜별 그룹핑. 일반 daily는 `target_date=today`.

---

## ADR-012: Silent Signals 개념

**결정**: 컨퍼런스 사후 분석에서 "예상했으나 미발표된 항목"을 명시적으로 추적.

**투자 철학**: 발표된 것만큼 *발표되지 않은 것*이 중요. 기술 로드맵 지연, 전략 변경, 규제 장벽의 신호.

**구현**: pre_event에서 `expected_items` JSON 생성 → post_event에서 실제 수집 데이터와 비교 → `silent_signals` JSON으로 저장 + LLM 투자 해석.

---

## ADR-013: 워드바운더리 키워드 매칭

**결정**: `re.compile(r'\b' + re.escape(kw) + r'\b', re.IGNORECASE)` 정규식 매칭.

**맥락**: "AI"가 "SAID", "FAIR"에 매칭되면 트렌드 데이터 오염. 멀티워드("mixture of experts")는 자연스럽게 구문 매칭.

**한계**: 한국어 키워드는 `\b`가 제대로 작동하지 않음. 현재 영문 키워드 중심 설계.
