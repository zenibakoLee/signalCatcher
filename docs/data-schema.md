# Data Schema

SQLite, WAL 모드, `data/signalcatcher.db`. 대시보드는 read-only 연결.

## 테이블 관계

```
raw_items ──1:1──> scored_items     (raw_item_id FK)
raw_items <──N:M── keyword_mentions (sample_item_ids JSON)
keyword_mentions ──agg──> keyword_daily_aggregates
keyword_daily_aggregates ──z-score──> trend_alerts
keyword_mentions ──pair count──> keyword_cooccurrences
scored_items + trend_alerts ──LLM──> digests
scored_items.related_tickers ──agg──> company_analyses (모멘텀 분석)
conferences.yaml ──LLM──> conference_briefings
```

## raw_items — 수집 원본
```sql
(source, source_id) UNIQUE  -- 멱등성 핵심
-- source: 'hackernews'|'arxiv'|'github'|'rss'|'youtube'|'reddit'
-- metadata: JSON (소스별 추가 필드: stars, comments, categories 등)
--   reddit metadata: subreddit, score, num_comments, upvote_ratio, link_flair_text, external_url
--   youtube metadata: channel_id, channel_name, thumbnail (채널 수집), search_query (검색 수집)
-- content_snippet: 본문/초록 첫 500자
-- collected_at: UTC. 기존 SQLite strftime 기본값의 offset 없는 값만 UTC로 해석한다.
--   신규 앱 쓰기와 신규 스키마 기본값은 +00:00이 포함된 ISO 8601을 저장한다.
-- published_at: offset이 있는 값만 절대시각으로 정렬한다. timezone 근거가 없는
--   기존 naive 값은 UTC로 추정하지 않고 collected_at을 정렬 fallback으로 사용한다.
```

## scored_items — LLM 스코어링
```sql
raw_item_id UNIQUE FK  -- 재스코어링 시 raw_items 보존
-- score: 0-100, category: breakthrough|trend|product|research|infrastructure|policy
-- title_ko: 한국어 번역 제목 (스코어링 시 자동 생성, translate-titles로 backfill)
-- related_tickers: JSON 배열, 관련 종목 1~3개 (예: '["NVDA", "삼성전자"]'). 기업분석의 입력 데이터.
-- 실패 시 fallback score=50
-- YouTube search 결과는 25% 감점 적용
```

## keyword_mentions — 소스별 일별 키워드 매칭
```sql
(keyword, source, mention_date) UNIQUE
-- mention_count: 해당 날짜 해당 소스에서 매칭 횟수
-- sample_item_ids: JSON, 매칭된 raw_item ID (최대 5개)
```

## keyword_daily_aggregates — 비정규화 합산
```sql
(keyword, mention_date) UNIQUE
-- total_count: 소스 통합 합계
-- source_breakdown: JSON {"hackernews": 5, "arxiv": 2, "reddit": 3}
```
트렌드 쿼리 최적화 목적. keyword_mentions에서 매일 집계.

## trend_alerts — z-score 임계값 초과 + 장기 가속
```sql
(keyword, alert_date) UNIQUE
-- z_score = (today_count - avg_30d) / std_30d
-- severity: 'notable' (z>2.0) | 'urgent' (z>3.0) | 'accelerating' (4주 연속 상승)
-- MIN_HISTORY_DAYS=7 미만이면 skip (콜드스타트 보호)
-- llm_interpretation: Claude Haiku 한국어 해석
```

## keyword_cooccurrences — 키워드 동시출현
```sql
(keyword_a, keyword_b, mention_date) UNIQUE
-- 같은 아이템에서 2개 이상 키워드가 매칭될 때 쌍별 카운트
-- 대시보드 네트워크 그래프의 데이터 소스
```

## digests — 일일 다이제스트
```sql
digest_date UNIQUE
-- summary_md: 전체 마크다운 (headline + 항목별 commentary + 트렌드 해석)
-- top_item_ids, trend_alert_ids: JSON 배열
```

## conference_briefings — 컨퍼런스 분석
```sql
(conference_name, conference_start, briefing_type) UNIQUE
-- briefing_type: 'pre_event' | 'post_event'
-- expected_items: JSON (pre에서 생성, post에서 비교 대상)
-- silent_signals: JSON (post_event만, 예상했으나 미발표 항목)
```

## keywords — 관리형 키워드
```sql
keyword UNIQUE
-- category: ai_model|hardware|framework|concept|company|infrastructure
-- status: active|retired
-- added_by: 'manual' | 'yaml_seed' | 'auto_discovery' | 'auto_promoted' | 'auto_activated' | 'spike_detection' | 'llm_suggestion'
```
매일 자동 관리: 신규 발견(auto_discovery), 스파이크 감지(spike_detection), 30일 무언급 은퇴.

## company_analyses — 기업 모멘텀 분석
```sql
(ticker, generated_at) UNIQUE
-- ticker: 종목 코드 (NVDA, 삼성전자 등)
-- company_name: 회사명
-- market: 'US' | 'KR'
-- signal_count: 분석에 사용된 시그널 수
-- signal_window_days: 시그널 수집 윈도우 (기본 30일)
-- momentum_score: 0-100 (70+ = 강한 모멘텀)
-- verdict: '강한 모멘텀' | '관심 관찰' | '모멘텀 약화' | '경고'
-- verdict_summary: 2~3문장 요약
-- five_questions: JSON, 5대 질문 프레임워크 진단 결과
-- signal_timeline: JSON, 핵심 이벤트 타임라인
-- risk_factors: JSON, 리스크 요인 목록
-- key_signals_json: JSON, 근거 시그널 + 웹 기사 + fake_or_real 판단 + action_note
-- model_used: 사용 LLM 모델 (claude-sonnet-4-6)
-- delivered: 0|1, Discord 전송 여부
```

## pipeline_runs — 실행 감사 로그
```sql
-- run_type: daily|event_pre|event_post|backfill|superstar_weekly
-- status: running|completed|completed_with_errors|failed
-- superstar_weekly: input_items, candidates_considered, candidates_published
-- current Superstar contract requires candidates_published=0
```

## weekly_superstar_snapshots / stage_evidence — CUDA 6단계 감사

기존 v1 테이블을 유지한다. 후보별 6개 플랫폼 단계의 `proven|missing`,
원본 `raw_item_id`, source, UTC source date를 보존한다. 현재 v1은
`score`/`rank`가 항상 `NULL`이고 발행하지 않는다.

## weekly_superstar_filter_audits — 문서 기반 v2 필터 헤더

```sql
-- weekly_snapshot_id UNIQUE FK: 기존 6단계 snapshot에 1:1로 추가
-- filter_version, document_source_url, document_source_version: 계약 출처
-- industry_track: software_platform|semiconductor_industrial|
--                 energy_infrastructure|other_unclassified
-- classification: 권위 있는 측정치가 없으면 NULL
-- classification_status: 현재 unclassified
-- authoritative_measurements_present: 현재 0
-- display_ready: self_reinforcing_moat 지원이 없으면 0, 행은 보존
-- missing_evidence: 재무 4~8Q/valuation/경영진 실행 등 release gap JSON
```

## weekly_superstar_filter_criteria / filter_citations — 정규화된 v2 근거

```sql
-- filter_criteria: 모든 공통 9개 + 선택 산업 track 전체 criterion을 순서대로 저장
-- status: supported|partial|missing
-- claim: missing이면 빈 문자열
-- missing_evidence: criterion별 명시적 gap JSON
-- filter_citations: criterion FK + exact raw_item_id/source/source_date
```

Terra는 위 dense 행이나 회사명을 직접 생성하지 않는다. 검증된 Luna ticker→회사명
map이 회사를 결정한다. Terra는 후보별로 최대 2개의 `supported|partial` sparse
finding만 반환하며 각 finding은 64자 printable-ASCII claim과 정확히 1개 citation만
포함한다. gap 문자열은 모델 출력이 아니다. Post-validation이 잘못된 track, 후보,
날짜, 중복 또는 불완전한 finding을 deterministic gap이 있는 `missing`으로 내린 뒤
공통 9개와 해당 산업 criterion 전체를 canonical order로 materialize한다.

뉴스/LLM 주장을 공식 재무·valuation·경영진 측정치로 승격하지 않는다.
`score`, `rank`, 추천, 주가수익률 순위는 v2 스키마에 없다.
