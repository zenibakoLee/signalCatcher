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

**결정**: 일일 스코어링·다이제스트·트렌드 해석·키워드 발견은 Haiku. 컨퍼런스 브리핑·기업 모멘텀 분석은 Sonnet.

**맥락**: 일일 50~100개 아이템 × 매일. Haiku로 비용 최소화. 컨퍼런스와 기업분석은 빈도 낮고 투자 판단 직결이라 Sonnet (claude-sonnet-4-6).

**배치 전략**: 10개씩 묶어 단일 API 호출 (20 → 10으로 축소, 품질 향상). JSON 응답 파싱 실패 시 개별 fallback(score=50).

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

## ADR-009: YouTube playlistItems 기본 + 제한적 Search API 병행

**결정**: YouTube Data API의 `playlistItems` 엔드포인트를 기본(1유닛/호출)으로 사용하되, 핵심 투자 쿼리 4개에 한해 `search` 엔드포인트(100유닛/호출)를 병행.

**맥락**: `playlistItems`만으로는 등록 채널의 최근 영상만 수집 가능. "NVIDIA Jensen Huang", "AI conference keynote" 등 특정 투자 관련 키워드의 크로스-채널 검색이 불가능. 일일 할당량 10,000유닛에서 채널 12개(12유닛) + 검색 4개(400유닛) = 약 412유닛으로 충분히 여유.

**구현**: `YouTubeCollector.__init__`에 `search_queries` 파라미터 추가. `_search_videos()` 메서드로 search API 호출. `seen_ids` set으로 채널 수집과 검색 수집 간 중복 제거.

**트레이드오프**: 검색 쿼리 수를 4개로 제한하여 할당량 관리. 쿼리 추가 시 일일 할당량 소비율 모니터링 필요.

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

---

## ADR-020: Reddit 서브레딧 수집 추가

**결정**: Reddit JSON API(`/r/{sub}/hot.json`)를 사용하여 11개 서브레딧에서 투자 신호를 수집.

**맥락**: Reddit은 기술 커뮤니티(r/MachineLearning, r/nvidia)와 투자 커뮤니티(r/wallstreetbets, r/investing)의 실시간 반응을 동시에 포착할 수 있는 유일한 소스. 기존 5개 소스(HN, arXiv, GitHub, RSS, YouTube)는 공식 발표와 학술 논문 중심으로, 커뮤니티의 체감 반응과 투자 심리를 놓침.

**구현**: `RedditCollector`(115줄)가 `BaseCollector` ABC를 상속. 각 서브레딧에서 hot 25개 조회, stickied 제외, since 이후 필터. metadata에 subreddit, score, num_comments, upvote_ratio, link_flair_text 저장. Rate limit 1.5 req/s.

**인증**: Reddit JSON API는 인증 없이 사용 가능(User-Agent만 필요). OAuth 불필요로 운영 복잡성 최소.

**대안**: Pushshift(서비스 불안정), Reddit OAuth API(과잉, 개인 프로젝트에 앱 등록 불필요), PRAW(동기 라이브러리, asyncio 미지원).

---

## ADR-021: 컨퍼런스 브리핑 윈도우 확장

**결정**: pre-event 윈도우를 `start_date - 2` ~ `start_date - 1`로, post-event 윈도우를 `end_date + 1` ~ `end_date + 3`으로 확장.

**맥락**: 기존에는 pre-event가 `end_date - 1` 정확히 1일, post-event가 `end_date + 1` 정확히 1일로 고정. 다일간 컨퍼런스(예: Computex 4일)의 경우, 시작 전날에만 트리거되면 시차/일정으로 생성 실패 시 재시도 불가. post-event도 다음날 파이프라인 장애 시 놓침.

**결과**: pre-event는 시작 2일 전부터 전날까지(2일 윈도우), post-event는 종료 다음날부터 3일 후까지(3일 윈도우). 이미 생성된 브리핑은 `UNIQUE(conference_name, conference_start, briefing_type)` 제약으로 중복 방지. 멱등성 유지.

---

## ADR-022: 기업 모멘텀 분석 (5대 질문 프레임워크)

**결정**: 시그널에 누적된 관련종목(related_tickers)을 집계하여 모멘텀 상위 종목을 자동 선정, Claude Sonnet으로 구조화된 분석 레포트를 생성.

**맥락**: 개별 시그널의 점수와 카테고리만으로는 "이 종목에 투자해야 하는가?"에 답할 수 없음. 시그널을 종목 단위로 집계하고, 5대 질문(장기 투자, 경쟁 구도, 수요 상한, 공급 병목, 가격 반영 시차) + "테마 vs 청구서" 판단을 통해 투자 판단에 직접 활용 가능한 레포트 생성.

**구현**: `find_momentum_candidates()` — `scored_items.related_tickers`에서 종목별 시그널 수, 평균 점수, 고점수 비율, 소셜 버즈를 가중 합산하여 우선순위 결정. `_fetch_news()` — Google News RSS로 최신 뉴스/실적 기사 보강. `generate_analysis()` — Claude Sonnet에 시그널+뉴스를 전달하여 JSON 레포트 생성.

**모델**: Claude Sonnet (Haiku 대비 깊은 추론 필요). 일일 최대 5건으로 비용 제어.

**대시보드 연동**: 목록 페이지(모멘텀 카드 그리드), 상세 페이지(5대 질문, 타임라인, 리스크, 근거 시그널, 참고 기사). 온디맨드 생성 API로 사용자가 임의 종목 분석 요청 가능.

---

## ADR-023: YouTube 자막 보강 (yt-dlp)

**결정**: YouTube 영상의 자동 생성 자막(en/ko)을 `yt-dlp`로 추출하여 `content_snippet`에 추가.

**맥락**: YouTube 영상의 기본 `content_snippet`은 영상 설명(description)만 포함하여 대부분 짧거나 무의미. 자막을 추가하면 스코어링 LLM이 영상의 실제 내용을 파악하여 정확도 향상.

**구현**: `enrich_youtube_transcripts()` — 수집 직후 실행. 기존 snippet이 600자 미만인 항목만 대상. VTT 파싱으로 타임스탬프 제거 후 텍스트만 추출. 최대 4000자.

**트레이드오프**: yt-dlp 외부 의존성. 30초 타임아웃으로 실패 시 건너뜀. 배치 제한 30개.

---

## ADR-024: 관련종목 매핑 (스코어링 시)

**결정**: 스코어링 시 LLM이 각 시그널에 관련 종목(티커) 1~3개를 함께 반환하도록 프롬프트 확장.

**맥락**: 기존에는 시그널을 수동으로 종목과 연결해야 했음. 스코어링 시점에 자동으로 매핑하면 기업 분석의 입력 데이터가 자동 축적됨.

**구현**: `scoring_prompt.txt`에 `related_tickers` 필드 추가. 미국 종목은 티커(NVDA), 한국 종목은 정식명(삼성전자). `scored_items` 테이블에 `related_tickers TEXT` 컬럼 추가 (마이그레이션).

---

## ADR-025: 주간 파이프라인 삭제 → 일일 키워드 자동 관리

**결정**: 주간 파이프라인(`weekly` CLI, `suggest_keywords()`, `deliver_keyword_suggestions()`)을 삭제하고, 키워드 관리를 일일 파이프라인에 통합.

**맥락**: 주간 "제안 → 승인" 워크플로우는 수동 개입이 필요하여 무인 운영 원칙에 위배. 매일 자동으로 발견/활성화/은퇴하는 방식이 더 적합.

**새 흐름**:
- `_discover_and_activate()`: 최근 7일 수집 데이터에서 미추적 구문을 추출 → LLM 평가(최대 5개, 보수적 선별) → 즉시 active 등록
- `_detect_spike_keywords()`: 오늘 언급이 14일 평균의 3배 이상인 미추적 키워드를 spike_detection으로 활성화
- `_retire_stale()`: 30일 무언급 active 키워드를 retired로 전환
- `MAX_ACTIVE_KEYWORDS=200`으로 상한 관리

---

## ADR-026: 스코어링 배치 축소 및 snippet 확장

**결정**: 스코어링 배치 크기를 20 → 10으로 축소, `content_snippet` 전달 길이를 300자 → 2000자로 확장.

**맥락**: 배치가 클수록 LLM의 개별 항목 분석 품질이 저하. snippet을 늘리면 LLM이 더 정확한 점수와 추론을 생성. 자막 보강으로 YouTube snippet이 길어진 것도 반영.

**YouTube 감점**: `search` API로 수집된 YouTube 항목은 채널 구독 기반 수집보다 노이즈가 많아 25% 감점(`score * 0.75`).

---

## ADR-027: 일일 파이프라인 재시도 래퍼

**결정**: launchd plist가 Python을 직접 호출하지 않고 `scripts/run-daily.sh` 래퍼를 통해 실행. 최대 3회 재시도(5분 간격), 전부 실패 시 Discord 알림.

**맥락**: 네트워크 장애, API 일시 오류 등으로 파이프라인이 간헐적으로 실패. 단일 실행 실패로 하루 데이터를 놓치면 투자 신호 감지에 공백 발생. 래퍼로 자동 재시도하여 내결함성 향상.

---

## ADR-028: Discord embed에 대시보드 URL 자동 포함

**결정**: 모든 Discord embed footer에 현재 Cloudflare Tunnel URL을 포함.

**맥락**: Discord에서 시그널/다이제스트를 확인한 후 바로 대시보드로 이동할 수 있어야 함. `data/logs/tunnel-url.txt`에서 현재 URL을 읽어 footer에 추가.

**구현**: `_dashboard_url()` 헬퍼 함수가 URL 파일을 읽고, 각 deliver 함수의 footer에 포함.

---

## ADR-029: 스코어링 프롬프트 — 비전공자 대상 언어

**결정**: 스코어링/다이제스트 프롬프트의 대상 독자를 "기술 비전공자인 개인 투자자"로 변경. 전문 용어 대신 쉬운 표현 사용 지시.

**맥락**: reasoning이 기술 전문가 수준으로 작성되면 사용자(매크로 투자자)가 바로 활용하기 어려움. "이 기술이 왜 돈이 되는지" 관점으로 쉽게 설명하도록 변경.
