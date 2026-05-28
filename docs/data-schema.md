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
```

## scored_items — LLM 스코어링
```sql
raw_item_id UNIQUE FK  -- 재스코어링 시 raw_items 보존
-- score: 0-100, category: breakthrough|trend|product|research|infrastructure|policy
-- title_ko: 한국어 번역 제목 (스코어링 시 자동 생성, translate-titles로 backfill)
-- 실패 시 fallback score=50
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
-- status: active|suggested|rejected|retired
-- added_by: 'manual' | 'llm_suggestion'
```

## pipeline_runs — 실행 감사 로그
```sql
-- run_type: daily|weekly|event_pre|event_post|backfill
-- status: running|completed|completed_with_errors|failed
```
