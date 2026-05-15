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

---

## ADR-014: 장기 가속 감지 (4주 연속 상승)

**결정**: z-score 스파이크 외에, 4주 연속 주간 평균 상승을 `accelerating` severity로 분류.

**맥락**: z-score는 단기 급등만 감지. 투자에서 중요한 것은 *점진적이지만 지속적인* 상승 추세. NVIDIA의 CUDA 생태계처럼 수개월에 걸친 가속 패턴.

**구현**: 35일 데이터에서 4주간 주간 평균 계산 → 4주 연속 상승 + 최소 weekly avg 1.0 → severity="accelerating", z_score=growth_rate.

---

## ADR-015: 키워드 동시출현 추적

**결정**: 같은 아이템에서 2개 이상 키워드가 매칭되면 쌍별 카운트를 `keyword_cooccurrences` 테이블에 저장.

**맥락**: 개별 키워드 빈도만으로는 기술 간 *관계 변화*를 포착 불가. "NVIDIA"와 "robotics"가 함께 언급되기 시작하면 새로운 투자 테마 신호.

**시각화**: 대시보드 트렌드 페이지에서 SVG 네트워크 그래프로 표시. 노드 크기=총 동시출현 횟수, 엣지 두께=쌍별 빈도.

---

## ADR-016: arXiv/YouTube 수집 윈도우 확장 (24시간 → 7일)

**결정**: arXiv와 YouTube 컬렉터의 날짜 필터를 `since - 6일`로 확장.

**맥락**: arXiv 논문은 제출 후 며칠 뒤에 검색 결과에 나타남. YouTube 채널도 매일 업로드하지 않음. 24시간 윈도우에서는 모든 항목이 필터링되어 수집량 0.

**안전장치**: `UNIQUE(source, source_id)` 제약으로 중복 삽입 방지. 같은 논문/영상이 여러 번 수집되어도 무시됨.

---

## ADR-017: Cloudflare Quick Tunnel (도메인 불필요 외부 접속)

**결정**: 도메인 구매 없이 Cloudflare Quick Tunnel로 대시보드 외부 접속.

**맥락**: 개인 투자 대시보드에 도메인 비용 불필요. Quick Tunnel은 재시작 시 URL 변경되지만, 시작 스크립트가 Discord에 새 URL 자동 전송.

**트레이드오프**: URL 비고정, Cloudflare SLA 없음. 향후 도메인 필요 시 Named Tunnel로 마이그레이션 가능.

---

## ADR-018: GitHub 수집 품질 필터 강화

**결정**: GitHub 키워드 검색 `stars:>5` → `stars:>50`, 트렌딩 `stars:>100` → `stars:>500`. "rising stars"(30일 이내 생성 stars:>100)와 "major releases"(stars:>1000 최근 푸시) 쿼리 추가.

**맥락**: 낮은 star 임계값으로 비인기 레포가 대량 수집되어 투자 신호로서 가치 없음. 투자 기회 신호에는 이미 검증된 프로젝트의 활동 변화가 더 유의미.

---

## ADR-019: DB 파일 mtime 기반 자동 새로고침

**결정**: 대시보드에서 3초마다 SQLite DB 파일의 mtime을 폴링, 변경 시 `router.refresh()`로 Server Components 재실행.

**맥락**: 파이프라인이 DB에 새 데이터를 쓸 때 대시보드가 자동 반영되어야 함. Turbopack의 HMR은 소스코드 변경만 감지하고 데이터 변경은 감지 불가.

**대안**: WebSocket(과잉), nodemon 재시작(전체 리로드로 UX 저하). mtime 폴링은 가볍고 변경 시에만 refresh.
