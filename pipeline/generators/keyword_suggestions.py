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

DISCOVERY_WINDOW_DAYS = 7
MIN_FREQUENCY = 3
RETIRE_ZERO_DAYS = 30
SPIKE_MULTIPLIER = 3
MAX_ACTIVE_KEYWORDS = 200


def auto_manage_keywords(target_date: date | None = None) -> dict:
    if target_date is None:
        target_date = date.today()

    conn = get_connection()
    retired = _retire_stale(conn, target_date)
    added = _discover_and_activate(conn, target_date)
    spiked = _detect_spike_keywords(conn, target_date)

    reactivated = {a["keyword"].lower() for a in added} | {s["keyword"].lower() for s in spiked}
    retired = [kw for kw in retired if kw.lower() not in reactivated]

    result = {
        "added": added,
        "spiked": spiked,
        "retired": retired,
    }
    logger.info(
        "Keyword management: added=%d, spike_added=%d, retired=%d",
        len(added), len(spiked), len(retired),
    )
    return result


def _discover_and_activate(conn, target_date: date) -> list[dict]:
    active_keywords = set(kw.lower() for kw in get_active_keywords(conn))

    active_count = len(active_keywords)
    if active_count >= MAX_ACTIVE_KEYWORDS:
        logger.info("Keyword discovery: at capacity (%d/%d)", active_count, MAX_ACTIVE_KEYWORDS)
        return []

    since = (target_date - timedelta(days=DISCOVERY_WINDOW_DAYS)).isoformat()
    until = (target_date + timedelta(days=1)).isoformat()

    rows = conn.execute(
        "SELECT title, content_snippet FROM raw_items WHERE collected_at >= ? AND collected_at < ?",
        (since, until),
    ).fetchall()

    if not rows:
        return []

    candidates = _extract_candidates(rows, active_keywords)
    if not candidates:
        return []

    evaluated = _evaluate_with_llm(candidates, active_keywords)

    activated = []
    for kw_info in evaluated:
        kw = kw_info["keyword"]
        if kw.lower() in active_keywords:
            continue
        conn.execute(
            """INSERT INTO keywords (keyword, category, added_by, status)
               VALUES (?, ?, 'auto_discovery', 'active')
               ON CONFLICT(keyword) DO UPDATE SET status = 'active', added_by = 'auto_discovery'""",
            (kw, kw_info.get("category", "concept")),
        )
        active_keywords.add(kw.lower())
        activated.append(kw_info)

    if activated:
        conn.commit()
        logger.info("Auto-activated keywords: %s", [a["keyword"] for a in activated])
    return activated


def _detect_spike_keywords(conn, target_date: date) -> list[dict]:
    active_keywords = set(kw.lower() for kw in get_active_keywords(conn))
    today = target_date.isoformat()
    prev_start = (target_date - timedelta(days=14)).isoformat()
    prev_end = (target_date - timedelta(days=1)).isoformat()

    rows = conn.execute(
        """SELECT keyword, total_count FROM keyword_daily_aggregates
           WHERE mention_date = ?""",
        (today,),
    ).fetchall()

    spiked = []
    for row in rows:
        kw = row["keyword"]
        if kw.lower() in active_keywords:
            continue

        avg_row = conn.execute(
            """SELECT AVG(total_count) as avg_count FROM keyword_daily_aggregates
               WHERE keyword = ? AND mention_date BETWEEN ? AND ?""",
            (kw, prev_start, prev_end),
        ).fetchone()

        avg_count = avg_row["avg_count"] or 0
        if row["total_count"] >= max(MIN_FREQUENCY, avg_count * SPIKE_MULTIPLIER):
            conn.execute(
                """INSERT INTO keywords (keyword, category, added_by, status)
                   VALUES (?, 'concept', 'spike_detection', 'active')
                   ON CONFLICT(keyword) DO UPDATE SET status = 'active', added_by = 'spike_detection'""",
                (kw,),
            )
            active_keywords.add(kw.lower())
            spiked.append({"keyword": kw, "today_count": row["total_count"], "avg_count": round(avg_count, 1)})

    if spiked:
        conn.commit()
        logger.info("Spike-activated keywords: %s", [s["keyword"] for s in spiked])
    return spiked


def _retire_stale(conn, target_date: date) -> list[str]:
    cutoff = (target_date - timedelta(days=RETIRE_ZERO_DAYS)).isoformat()
    rows = conn.execute(
        """SELECT k.keyword FROM keywords k
           WHERE k.status = 'active' AND k.added_by != 'manual'
           AND NOT EXISTS (
               SELECT 1 FROM keyword_daily_aggregates a
               WHERE lower(a.keyword) = lower(k.keyword) AND a.mention_date >= ?
           )""",
        (cutoff,),
    ).fetchall()

    if not rows:
        return []

    keywords = [r["keyword"] for r in rows]
    conn.executemany(
        "UPDATE keywords SET status = 'retired' WHERE keyword = ?",
        [(kw,) for kw in keywords],
    )
    conn.commit()
    logger.info("Retired stale keywords: %s", keywords)
    return keywords


def _extract_candidates(rows: list, active_keywords: set[str]) -> list[tuple[str, int]]:
    word_freq: Counter = Counter()

    bigram_pattern = re.compile(r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)\b")
    tech_pattern = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:[-\.][A-Za-z0-9]+)*)\b")
    quoted_pattern = re.compile(r'"([^"]{3,40})"')
    stopwords = {"the", "and", "for", "with", "from", "this", "that", "are", "was", "has", "not"}

    for row in rows:
        text = (row["title"] or "") + " " + (row["content_snippet"] or "")

        for match in bigram_pattern.findall(text):
            term = match.strip()
            if 2 <= len(term.split()) <= 4 and term.lower() not in active_keywords:
                word_freq[term] += 1

        for match in tech_pattern.findall(text):
            if len(match) >= 3 and match.lower() not in active_keywords and match.lower() not in stopwords:
                word_freq[match] += 1

        for match in quoted_pattern.findall(text):
            if match.lower() not in active_keywords:
                word_freq[match] += 1

    filtered = [
        (term, count) for term, count in word_freq.most_common(100)
        if count >= MIN_FREQUENCY
    ]
    return filtered[:30]


def _evaluate_with_llm(candidates: list[tuple[str, int]], active_keywords: set[str]) -> list[dict]:
    candidate_lines = [f"- \"{term}\" ({count}회 등장)" for term, count in candidates]

    prompt = f"""당신은 기술 투자 신호 추적 시스템의 키워드 관리자입니다.

현재 추적 중인 키워드: {', '.join(sorted(active_keywords)[:30])}...

아래는 지난 7일간 수집된 콘텐츠에서 자주 등장했지만 아직 추적하지 않는 구문입니다:
{chr(10).join(candidate_lines)}

위 후보 중에서 기술 투자 신호 추적에 **확실히 가치가 있는** 키워드만 선별하세요.
기준:
- 특정 기술, 제품, 기업, 트렌드를 명확히 나타내는 것만 선택
- 이미 추적 중인 키워드와 중복되거나 너무 일반적인 것은 제외
- 투자 관점에서 추적할 가치가 높은 것을 우선
- 보수적으로 선별 (불확실하면 제외)

JSON 배열로 반환:
[{{"keyword": "키워드", "category": "ai_model|hardware|framework|concept|company|infrastructure", "reason": "추가 이유 (한국어, 15자 이내)"}}]

최대 5개까지만. 유효한 JSON만 반환하세요."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
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
        logger.exception("Keyword discovery: LLM evaluation failed")
        return []
