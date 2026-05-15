from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, timedelta

import anthropic

from pipeline.db import get_connection, get_active_keywords

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"
MIN_FREQUENCY = 3
TOP_CANDIDATES = 30


def suggest_keywords(target_date: date | None = None) -> list[dict]:
    if target_date is None:
        target_date = date.today()

    conn = get_connection()
    active_keywords = set(kw.lower() for kw in get_active_keywords(conn))

    since = (target_date - timedelta(days=7)).isoformat()
    until = (target_date + timedelta(days=1)).isoformat()

    rows = conn.execute(
        "SELECT title, content_snippet FROM raw_items WHERE collected_at >= ? AND collected_at < ?",
        (since, until),
    ).fetchall()

    if not rows:
        logger.info("Keyword suggestions: no items from last 7 days")
        return []

    candidates = _extract_candidates(rows, active_keywords)

    if not candidates:
        logger.info("Keyword suggestions: no new candidates found")
        return []

    suggestions = _evaluate_with_llm(candidates, active_keywords)

    _store_suggestions(conn, suggestions)

    logger.info("Keyword suggestions: %d new keywords suggested", len(suggestions))
    return suggestions


def _extract_candidates(rows: list, active_keywords: set[str]) -> list[tuple[str, int]]:
    word_freq: Counter = Counter()

    bigram_pattern = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
    tech_pattern = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[-\.][A-Za-z0-9]+)*)\b")
    quoted_pattern = re.compile(r'"([^"]{3,40})"')

    for row in rows:
        text = (row["title"] or "") + " " + (row["content_snippet"] or "")

        for match in bigram_pattern.findall(text):
            term = match.strip()
            if 2 <= len(term.split()) <= 4 and term.lower() not in active_keywords:
                word_freq[term] += 1

        for match in tech_pattern.findall(text):
            if len(match) >= 3 and match.lower() not in active_keywords:
                if not match.lower() in {"the", "and", "for", "with", "from", "this", "that", "are", "was", "has", "not"}:
                    word_freq[match] += 1

        for match in quoted_pattern.findall(text):
            if match.lower() not in active_keywords:
                word_freq[match] += 1

    filtered = [
        (term, count) for term, count in word_freq.most_common(100)
        if count >= MIN_FREQUENCY
    ]

    return filtered[:TOP_CANDIDATES]


def _evaluate_with_llm(candidates: list[tuple[str, int]], active_keywords: set[str]) -> list[dict]:
    candidate_lines = [f"- \"{term}\" ({count}회 등장)" for term, count in candidates]

    prompt = f"""당신은 기술 투자 신호 추적 시스템의 키워드 관리자입니다.

현재 추적 중인 키워드: {', '.join(sorted(active_keywords)[:30])}...

아래는 지난 7일간 수집된 콘텐츠에서 자주 등장했지만 아직 추적하지 않는 구문입니다:
{chr(10).join(candidate_lines)}

위 후보 중에서 기술 투자 신호 추적에 가치가 있는 키워드를 선별하세요.
- 이미 추적 중인 키워드와 중복되거나 너무 일반적인 것은 제외
- 특정 기술, 제품, 기업, 트렌드를 나타내는 것을 선호
- 각 키워드에 적절한 카테고리를 지정

JSON 배열로 반환:
[{{"keyword": "키워드", "category": "ai_model|hardware|framework|concept|company|infrastructure", "reason": "추가 이유 한 문장 (한국어)"}}]

최대 10개까지만 선별하세요. 유효한 JSON만 반환하세요."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            text = text[start : end + 1]

        return json.loads(text)
    except Exception:
        logger.exception("Keyword suggestions: LLM evaluation failed")
        return []


def _store_suggestions(conn, suggestions: list[dict]) -> None:
    for s in suggestions:
        conn.execute(
            """INSERT OR IGNORE INTO keywords (keyword, category, added_by, status)
               VALUES (?, ?, 'llm_suggestion', 'suggested')""",
            (s["keyword"], s.get("category", "concept")),
        )
    conn.commit()
