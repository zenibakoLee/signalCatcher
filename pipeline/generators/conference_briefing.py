from __future__ import annotations

import json
import logging
from datetime import date, timedelta

import anthropic
import yaml

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

PRE_EVENT_MODEL = "claude-sonnet-4-6"
POST_EVENT_MODEL = "claude-sonnet-4-6"
CONFIG_PATH = "config/conferences.yaml"


def load_conferences() -> list[dict]:
    with open(CONFIG_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("conferences", [])


def get_actionable_conferences(target_date: date | None = None) -> dict[str, list[dict]]:
    if target_date is None:
        target_date = date.today()

    conferences = load_conferences()
    conn = get_connection()
    result: dict[str, list[dict]] = {"pre_event": [], "post_event": []}

    for conf in conferences:
        start = date.fromisoformat(conf["start_date"])
        end = date.fromisoformat(conf["end_date"])
        name = conf["name"]

        pre_window_start = start - timedelta(days=2)
        post_window_start = end + timedelta(days=1)
        post_window_end = end + timedelta(days=3)

        if pre_window_start <= target_date < start:
            existing = conn.execute(
                "SELECT id FROM conference_briefings WHERE conference_name = ? AND conference_start = ? AND briefing_type = 'pre_event'",
                (name, conf["start_date"]),
            ).fetchone()
            if not existing:
                result["pre_event"].append(conf)

        if post_window_start <= target_date <= post_window_end:
            existing = conn.execute(
                "SELECT id FROM conference_briefings WHERE conference_name = ? AND conference_start = ? AND briefing_type = 'post_event'",
                (name, conf["start_date"]),
            ).fetchone()
            if not existing:
                result["post_event"].append(conf)

    return result


def generate_pre_event(conf: dict) -> dict | None:
    conn = get_connection()
    name = conf["name"]
    organizer = conf.get("organizer", "")
    topics = conf.get("expected_topics", [])
    search_terms = conf.get("search_terms", [])

    recent_signals = _gather_recent_signals(search_terms + topics, conf["start_date"])

    prompt = f"""당신은 기술 투자 분석가입니다. 모든 출력은 한국어로 작성하세요.

**{name}** ({conf['start_date']} ~ {conf['end_date']}, 주최: {organizer})이 곧 시작됩니다.

관련 키워드: {', '.join(topics)}
최근 관련 시그널:
{recent_signals}

아래 작업을 수행하세요:

1. 이 컨퍼런스에서 예상되는 주요 발표/공개 항목을 8-12개 나열하세요.
2. 각 항목에 대해 투자 관련성(높음/중간/낮음)을 평가하세요.
3. 각 항목에 대해 기대 근거를 한 문장으로 설명하세요.

JSON 형식으로 반환:
{{
  "headline": "컨퍼런스 사전 브리핑 한줄 헤드라인 (한국어, 80자 이내)",
  "summary": "전체 요약 2-3문장 (한국어)",
  "expected_items": [
    {{
      "item": "예상 발표 항목",
      "investment_relevance": "높음|중간|낮음",
      "reasoning": "기대 근거 한 문장"
    }}
  ],
  "watch_points": "투자자가 특히 주목해야 할 포인트 2-3개 (한국어)"
}}

유효한 JSON만 반환하세요."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=PRE_EVENT_MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        data = _parse_json(text)
    except Exception:
        logger.exception("Conference briefing: failed to generate pre-event for %s", name)
        return None

    content_md = _format_pre_event_md(data, conf)

    conn.execute(
        """INSERT INTO conference_briefings
           (conference_name, conference_start, conference_end, briefing_type,
            content_md, expected_items, model_used)
           VALUES (?, ?, ?, 'pre_event', ?, ?, ?)""",
        (
            name,
            conf["start_date"],
            conf["end_date"],
            content_md,
            json.dumps(data.get("expected_items", []), ensure_ascii=False),
            PRE_EVENT_MODEL,
        ),
    )
    conn.commit()

    logger.info("Conference briefing: pre-event generated for %s", name)
    return data


def generate_post_event(conf: dict) -> dict | None:
    conn = get_connection()
    name = conf["name"]
    organizer = conf.get("organizer", "")
    search_terms = conf.get("search_terms", [])
    topics = conf.get("expected_topics", [])

    pre_event = conn.execute(
        "SELECT expected_items FROM conference_briefings WHERE conference_name = ? AND conference_start = ? AND briefing_type = 'pre_event'",
        (name, conf["start_date"]),
    ).fetchone()

    expected_items = []
    if pre_event and pre_event["expected_items"]:
        try:
            expected_items = json.loads(pre_event["expected_items"])
        except json.JSONDecodeError:
            pass

    conference_items = _gather_conference_items(
        search_terms + topics,
        conf["start_date"],
        conf["end_date"],
    )

    expected_block = ""
    if expected_items:
        lines = [f"- {e['item']} (관련성: {e.get('investment_relevance', '?')})" for e in expected_items]
        expected_block = f"\n\n사전 예상 항목:\n" + "\n".join(lines)

    prompt = f"""당신은 기술 투자 분석가입니다. 모든 출력은 한국어로 작성하세요.

**{name}** ({conf['start_date']} ~ {conf['end_date']}, 주최: {organizer})이 종료되었습니다.
{expected_block}

컨퍼런스 기간 수집된 관련 콘텐츠:
{conference_items}

아래 작업을 수행하세요:

1. 컨퍼런스의 핵심 발표/공개 사항을 요약하세요.
2. 사전 예상 항목과 비교하여 실제 발표된 것과 빠진 것을 분석하세요.
3. **Silent Signals**: 예상했으나 발표되지 않은 항목들을 식별하고, 그 부재가 의미하는 바를 투자 관점에서 해석하세요.
4. 예상하지 못했으나 발표된 서프라이즈 항목을 식별하세요.

JSON 형식으로 반환:
{{
  "headline": "사후 브리핑 한줄 헤드라인 (한국어, 80자 이내)",
  "summary": "핵심 요약 2-3문장 (한국어)",
  "key_announcements": [
    {{"item": "발표 항목", "significance": "투자 관련 의미 한 문장"}}
  ],
  "silent_signals": [
    {{"expected_item": "예상했으나 미발표 항목", "interpretation": "부재의 투자적 의미 한 문장"}}
  ],
  "surprises": [
    {{"item": "예상 외 발표", "significance": "투자 관련 의미 한 문장"}}
  ],
  "investment_takeaway": "투자자를 위한 핵심 시사점 2-3문장 (한국어)"
}}

유효한 JSON만 반환하세요."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=POST_EVENT_MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        data = _parse_json(text)
    except Exception:
        logger.exception("Conference briefing: failed to generate post-event for %s", name)
        return None

    content_md = _format_post_event_md(data, conf)
    silent_signals = data.get("silent_signals", [])

    source_ids = _get_conference_item_ids(search_terms + topics, conf["start_date"], conf["end_date"])

    conn.execute(
        """INSERT INTO conference_briefings
           (conference_name, conference_start, conference_end, briefing_type,
            content_md, expected_items, silent_signals, source_item_ids, model_used)
           VALUES (?, ?, ?, 'post_event', ?, ?, ?, ?, ?)""",
        (
            name,
            conf["start_date"],
            conf["end_date"],
            content_md,
            json.dumps(expected_items, ensure_ascii=False) if expected_items else None,
            json.dumps(silent_signals, ensure_ascii=False) if silent_signals else None,
            json.dumps(source_ids) if source_ids else None,
            POST_EVENT_MODEL,
        ),
    )
    conn.commit()

    logger.info("Conference briefing: post-event generated for %s", name)
    return data


def _gather_recent_signals(terms: list[str], before_date: str) -> str:
    conn = get_connection()
    since = (date.fromisoformat(before_date) - timedelta(days=14)).isoformat()

    conditions = " OR ".join(["(r.title LIKE ? OR r.content_snippet LIKE ?)"] * len(terms))
    params = []
    for t in terms:
        like = f"%{t}%"
        params.extend([like, like])
    params.extend([since, before_date])

    rows = conn.execute(
        f"""SELECT r.title, r.source, r.url, r.content_snippet, s.score
           FROM raw_items r
           LEFT JOIN scored_items s ON s.raw_item_id = r.id
           WHERE ({conditions})
             AND r.published_at >= ? AND r.published_at < ?
           ORDER BY COALESCE(s.score, 0) DESC
           LIMIT 20""",
        params,
    ).fetchall()

    if not rows:
        return "(최근 관련 시그널 없음)"

    lines = []
    for r in rows:
        score = f"[Score: {r['score']}] " if r["score"] else ""
        snippet = (r["content_snippet"] or "")[:150]
        lines.append(f"- {score}[{r['source']}] {r['title']}\n  {snippet}")
    return "\n".join(lines)


def _gather_conference_items(terms: list[str], start_date: str, end_date: str) -> str:
    conn = get_connection()
    end_plus = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()

    conditions = " OR ".join(["(r.title LIKE ? OR r.content_snippet LIKE ?)"] * len(terms))
    params = []
    for t in terms:
        like = f"%{t}%"
        params.extend([like, like])
    params.extend([start_date, end_plus])

    rows = conn.execute(
        f"""SELECT r.title, r.source, r.url, r.content_snippet, s.score
           FROM raw_items r
           LEFT JOIN scored_items s ON s.raw_item_id = r.id
           WHERE ({conditions})
             AND r.published_at >= ? AND r.published_at < ?
           ORDER BY COALESCE(s.score, 0) DESC
           LIMIT 30""",
        params,
    ).fetchall()

    if not rows:
        return "(컨퍼런스 기간 관련 콘텐츠 없음 — 일일 수집 데이터 기반)"

    lines = []
    for r in rows:
        score = f"[Score: {r['score']}] " if r["score"] else ""
        snippet = (r["content_snippet"] or "")[:200]
        lines.append(f"- {score}[{r['source']}] {r['title']}\n  {snippet}")
    return "\n".join(lines)


def _get_conference_item_ids(terms: list[str], start_date: str, end_date: str) -> list[int]:
    conn = get_connection()
    end_plus = (date.fromisoformat(end_date) + timedelta(days=1)).isoformat()

    conditions = " OR ".join(["(title LIKE ? OR content_snippet LIKE ?)"] * len(terms))
    params = []
    for t in terms:
        like = f"%{t}%"
        params.extend([like, like])
    params.extend([start_date, end_plus])

    rows = conn.execute(
        f"SELECT id FROM raw_items WHERE ({conditions}) AND published_at >= ? AND published_at < ?",
        params,
    ).fetchall()
    return [r["id"] for r in rows]


def _format_pre_event_md(data: dict, conf: dict) -> str:
    parts = [
        f"# 📋 사전 브리핑: {conf['name']}",
        f"*{conf['start_date']} ~ {conf['end_date']}*",
        "",
        data.get("summary", ""),
        "",
        "## 예상 발표 항목",
    ]
    for item in data.get("expected_items", []):
        rel = item.get("investment_relevance", "?")
        icon = "🔴" if rel == "높음" else "🟡" if rel == "중간" else "🟢"
        parts.append(f"- {icon} **{item['item']}** ({rel})")
        parts.append(f"  {item.get('reasoning', '')}")

    if data.get("watch_points"):
        parts.extend(["", "## 투자자 주목 포인트", data["watch_points"]])

    return "\n".join(parts)


def _format_post_event_md(data: dict, conf: dict) -> str:
    parts = [
        f"# 📊 사후 브리핑: {conf['name']}",
        f"*{conf['start_date']} ~ {conf['end_date']}*",
        "",
        data.get("summary", ""),
        "",
        "## 핵심 발표",
    ]
    for a in data.get("key_announcements", []):
        parts.append(f"- **{a['item']}** — {a.get('significance', '')}")

    if data.get("silent_signals"):
        parts.extend(["", "## 🔇 Silent Signals (예상했으나 미발표)"])
        for s in data["silent_signals"]:
            parts.append(f"- **{s['expected_item']}** — {s.get('interpretation', '')}")

    if data.get("surprises"):
        parts.extend(["", "## ⚡ 서프라이즈"])
        for s in data["surprises"]:
            parts.append(f"- **{s['item']}** — {s.get('significance', '')}")

    if data.get("investment_takeaway"):
        parts.extend(["", "## 투자 시사점", data["investment_takeaway"]])

    return "\n".join(parts)


def _parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        raise
