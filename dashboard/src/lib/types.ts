export interface ScoredItem {
  id: number;
  title: string;
  title_ko: string | null;
  url: string | null;
  source: string;
  score: number;
  score_reasoning: string | null;
  category: string | null;
  content_snippet: string | null;
  published_at: string;
  metadata: string | null;
}

export interface TrendAlert {
  keyword: string;
  alert_date: string;
  z_score: number;
  severity: string;
  today_count: number;
  moving_avg_7d: number | null;
  moving_avg_30d: number | null;
  llm_interpretation: string | null;
}

export interface KeywordAggregate {
  keyword: string;
  mention_date: string;
  total_count: number;
  source_breakdown: string | null;
}

export interface Digest {
  digest_date: string;
  headline: string;
  summary_md: string;
}

export interface ConferenceBriefing {
  id: number;
  conference_name: string;
  conference_start: string;
  conference_end: string;
  briefing_type: string;
  content_md: string;
  expected_items: string | null;
  silent_signals: string | null;
  source_item_ids: string | null;
  generated_at: string;
}

export interface Keyword {
  id: number;
  keyword: string;
  category: string | null;
  added_by: string;
  status: string;
  added_at: string;
}
