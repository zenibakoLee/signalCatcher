from __future__ import annotations

from datetime import datetime
from pydantic import BaseModel


class RawItem(BaseModel):
    source: str
    source_id: str
    title: str
    url: str | None = None
    author: str | None = None
    content_snippet: str | None = None
    published_at: datetime
    metadata: dict | None = None


class ScoredItem(BaseModel):
    raw_item_id: int
    score: int
    score_reasoning: str
    category: str
    model_used: str


class TrendAlert(BaseModel):
    keyword: str
    alert_date: str
    z_score: float
    severity: str
    moving_avg_7d: float
    moving_avg_30d: float
    std_dev_30d: float
    today_count: int
    llm_interpretation: str | None = None
