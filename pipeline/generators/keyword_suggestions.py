from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, timedelta

from pipeline import llm
from pipeline.db import get_connection, get_active_keywords

logger = logging.getLogger(__name__)

MODEL = llm.LUNA_MODEL

DISCOVERY_WINDOW_DAYS = 7
MIN_FREQUENCY = 3
RETIRE_ZERO_DAYS = 30
SPIKE_MULTIPLIER = 3
MAX_ACTIVE_KEYWORDS = 200


def auto_manage_keywords(target_date: date | None = None, *, run_id: str | int) -> dict:
    if target_date is None:
        target_date = date.today()

    conn = get_connection()
    active_keywords = set(kw.lower() for kw in get_active_keywords(conn))
    since = (target_date - timedelta(days=DISCOVERY_WINDOW_DAYS)).isoformat()
    until = (target_date + timedelta(days=1)).isoformat()
    rows = conn.execute(
        "SELECT title, content_snippet FROM raw_items WHERE collected_at >= ? AND collected_at < ?",
        (since, until),
    ).fetchall()
    candidates = _extract_candidates(rows, active_keywords) if len(active_keywords) < MAX_ACTIVE_KEYWORDS else []
    evaluated = _evaluate_with_llm(candidates, active_keywords, conn=conn, run_id=run_id) if candidates else []
    try:
        conn.execute("BEGIN")
        resurged = _reactivate_resurgent(conn, target_date)
        retired = _retire_stale(conn, target_date)
        added = _discover_and_activate(conn, target_date, evaluated=evaluated)
        spiked = _detect_spike_keywords(conn, target_date)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    reactivated = {a["keyword"].lower() for a in added} | {s["keyword"].lower() for s in spiked} | {r.lower() for r in resurged}
    retired = [kw for kw in retired if kw.lower() not in reactivated]

    result = {
        "added": added,
        "spiked": spiked,
        "resurged": resurged,
        "retired": retired,
    }
    logger.info(
        "Keyword management: added=%d, spike_added=%d, resurged=%d, retired=%d",
        len(added), len(spiked), len(resurged), len(retired),
    )
    return result


RESURGE_MIN_MENTIONS = 8  # 최근 7일 raw_items 제목에 이만큼 등장하면 은퇴 키워드 부활


def _reactivate_resurgent(conn, target_date: date) -> list[str]:
    """은퇴한 키워드가 최근 다시 화제가 되면 재활성화.

    은퇴 키워드는 카운팅되지 않아 spike 감지로 부활할 수 없고, 신규 발굴 LLM은
    '이미 아는 회사'라며 넘긴다. DeepSeek처럼 조용하다 재부상하는 케이스가
    이 사각지대에 빠진다 (은퇴 후 investor meeting 인터뷰로 재점화). 은퇴
    키워드를 최근 raw_items 제목에서 단어 경계로 카운트해 임계 이상이면 되살린다.
    (부분일치는 'spark'→'DSpark' 같은 오탐을 낳으므로 단어 경계 매칭 필수)
    """
    since = (target_date - timedelta(days=DISCOVERY_WINDOW_DAYS)).isoformat()
    until = (target_date + timedelta(days=1)).isoformat()

    # 명명된 개체(회사·모델·하드웨어)만 대상 — framework/concept/infrastructure
    # 같은 일반명사(Python, Knowledge, Data Center)는 상시 빈출이라 오탐을 낳는다.
    ENTITY_CATEGORIES = ("company", "ai_model", "hardware", "breakthrough")
    retired_kws = conn.execute(
        f"SELECT keyword FROM keywords WHERE status = 'retired' AND category IN ({','.join('?' * len(ENTITY_CATEGORIES))})",
        ENTITY_CATEGORIES,
    ).fetchall()
    if not retired_kws:
        return []

    rows = conn.execute(
        "SELECT title FROM raw_items WHERE collected_at >= ? AND collected_at < ?",
        (since, until),
    ).fetchall()
    titles = [(r["title"] or "").lower() for r in rows]

    resurged = []
    for kw_row in retired_kws:
        kw = kw_row["keyword"]
        # 3글자 미만은 오탐 위험이 커서 제외
        if len(kw) < 3:
            continue
        pattern = re.compile(r"\b" + re.escape(kw.lower()) + r"\b")
        mentions = sum(1 for t in titles if pattern.search(t))
        if mentions >= RESURGE_MIN_MENTIONS:
            conn.execute(
                "UPDATE keywords SET status = 'active', added_by = 'resurgence' WHERE keyword = ?",
                (kw,),
            )
            resurged.append(kw)
            logger.info("Reactivated resurgent keyword '%s' (%d mentions in %dd)", kw, mentions, DISCOVERY_WINDOW_DAYS)
    return resurged


def _discover_and_activate(conn, target_date: date, *, evaluated: list[dict] | None = None) -> list[dict]:
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

    if evaluated is None:
        raise llm.LLMConfigurationError("keyword suggestions must be staged before persistence")

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


def _evaluate_with_llm(
    candidates: list[tuple[str, int]], active_keywords: set[str], *, conn=None, run_id: str | int
) -> list[dict]:
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

JSON 객체로 반환:
{{"keywords": [{{"keyword": "키워드", "category": "ai_model|hardware|framework|concept|company|infrastructure", "reason": "추가 이유 (한국어, 15자 이내)"}}]}}

최대 5개까지만. 유효한 JSON만 반환하세요."""

    keyword_entry = {
        "type": "object",
        "properties": {
            "keyword": {"type": "string", "minLength": 1, "maxLength": 80},
            "category": {
                "type": "string",
                "enum": ["ai_model", "hardware", "framework", "concept", "company", "infrastructure"],
            },
            "reason": {"type": "string", "minLength": 1, "maxLength": 30},
        },
        "required": ["keyword", "category", "reason"],
        "additionalProperties": False,
    }
    if conn is None:
        conn = get_connection()
    llm.require_no_business_transaction(conn)
    result = llm.get_boundary().complete(
        instructions="Select only valuable investment-signal keywords.",
        input_text=prompt,
        max_output_tokens=1000,
        model=MODEL,
        workload="keyword_discovery",
        run_id=run_id,
        output_schema=llm.strict_object_schema(
            "keyword_suggestions",
            {
                "keywords": {
                    "type": "array",
                    "maxItems": min(5, len(candidates)),
                    "items": keyword_entry,
                }
            },
        ),
    )
    selected = result.parsed["keywords"]
    candidate_names = {name.casefold() for name, _ in candidates}
    selected_names = [entry["keyword"].casefold() for entry in selected]
    if len(selected_names) != len(set(selected_names)) or any(name not in candidate_names for name in selected_names):
        raise llm.LLMParseError("keyword suggestions must be unique supplied candidates")
    return selected
