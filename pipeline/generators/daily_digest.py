from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"


def generate_digest(target_date: date | None = None) -> dict | None:
    if target_date is None:
        target_date = date.today()
    date_str = target_date.isoformat()

    conn = get_connection()

    existing = conn.execute(
        "SELECT id FROM digests WHERE digest_date = ?", (date_str,)
    ).fetchone()
    if existing:
        logger.info("Digest: already exists for %s", date_str)
        return None

    top_items = conn.execute(
        """SELECT s.raw_item_id, s.score, s.score_reasoning, s.category,
                  r.title, r.url, r.source, r.content_snippet
           FROM scored_items s
           JOIN raw_items r ON s.raw_item_id = r.id
           WHERE date(r.collected_at) = ?
           ORDER BY s.score DESC
           LIMIT 15""",
        (date_str,),
    ).fetchall()

    if not top_items:
        logger.warning("Digest: no scored items for %s", date_str)
        return None

    trend_alerts = conn.execute(
        """SELECT keyword, z_score, severity, today_count, moving_avg_30d
           FROM trend_alerts
           WHERE alert_date = ?
           ORDER BY z_score DESC""",
        (date_str,),
    ).fetchall()

    items_block = []
    for i, item in enumerate(top_items, 1):
        snippet = (item["content_snippet"] or "")[:200]
        items_block.append(
            f'{i}. [{item["source"].upper()} | Score: {item["score"]}] {item["title"]}\n'
            f'   URL: {item["url"] or "N/A"}\n'
            f'   Category: {item["category"]} | Reasoning: {item["score_reasoning"]}\n'
            f'   Snippet: {snippet}'
        )

    trends_block = ""
    if trend_alerts:
        trend_lines = []
        for alert in trend_alerts:
            trend_lines.append(
                f'- **{alert["keyword"]}**: z-score {alert["z_score"]:.1f} ({alert["severity"]}), '
                f'today {alert["today_count"]} vs 30d avg {alert["moving_avg_30d"]:.1f}'
            )
        trends_block = "\n\nTREND ALERTS:\n" + "\n".join(trend_lines)

    prompt = f"""당신은 {date_str}의 일일 투자 시그널 다이제스트를 작성하는 분석가입니다.
모든 출력은 반드시 한국어로 작성하세요.

아래는 오늘 기술 소스(HN, arXiv, GitHub, RSS, YouTube)에서 수집된 상위 항목입니다.
투자 기회 관련도에 따라 이미 점수가 매겨져 있습니다.{trends_block}

항목:
{chr(10).join(items_block)}

아래 JSON 형식으로 다이제스트를 생성하세요:
{{
  "headline": "오늘의 가장 중요한 시그널을 요약하는 한 줄 헤드라인 (최대 80자, 한국어)",
  "summary": "오늘의 핵심 테마와 시그널을 요약하는 2-3문장 (한국어)",
  "top_items_commentary": [
    {{"title": "항목 제목 (원문 유지)", "score": 85, "source": "HN", "url": "...", "commentary": "투자자에게 왜 중요한지 한 문장 설명 (한국어)"}}
  ],
  "trend_section": "트렌드 알림이 있으면 2-3문장으로 해석 (한국어). 없으면 빈 문자열.",
  "one_line_takeaway": "오늘의 가장 실행 가능한 인사이트 한 줄 (한국어)"
}}

유효한 JSON만 반환하세요."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=3000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        digest_data = _parse_json(text)
    except Exception:
        logger.exception("Digest generation failed, creating minimal digest")
        digest_data = {
            "headline": f"시그널 다이제스트 — {date_str}",
            "summary": f"{len(top_items)}개 항목 수집 완료. LLM 다이제스트 생성 실패.",
            "top_items_commentary": [],
            "trend_section": "",
            "one_line_takeaway": "항목을 수동으로 검토하세요.",
        }

    top_item_ids = [item["raw_item_id"] for item in top_items]
    alert_ids = [alert["keyword"] for alert in trend_alerts]

    summary_md = _format_markdown(digest_data)

    conn.execute(
        """INSERT INTO digests (digest_date, headline, summary_md, top_item_ids, trend_alert_ids, model_used)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            date_str,
            digest_data.get("headline", f"Digest {date_str}"),
            summary_md,
            json.dumps(top_item_ids),
            json.dumps(alert_ids),
            MODEL,
        ),
    )
    conn.commit()

    logger.info("Digest: generated for %s with %d items", date_str, len(top_items))
    return digest_data


def _format_markdown(data: dict) -> str:
    parts = [
        f"# {data.get('headline', 'Daily Digest')}",
        "",
        data.get("summary", ""),
        "",
    ]

    for item in data.get("top_items_commentary", []):
        score = item.get("score", "?")
        source = item.get("source", "?")
        title = item.get("title", "Untitled")
        url = item.get("url", "")
        commentary = item.get("commentary", "")
        link = f"[{title}]({url})" if url else title
        parts.append(f"- **[{score}|{source}]** {link} — {commentary}")

    if data.get("trend_section"):
        parts.extend(["", "## 트렌드 알림", data["trend_section"]])

    if data.get("one_line_takeaway"):
        parts.extend(["", f"**핵심 인사이트:** {data['one_line_takeaway']}"])

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
