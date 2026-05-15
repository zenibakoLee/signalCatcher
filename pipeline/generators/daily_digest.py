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

    prompt = f"""You are writing a daily investment signal digest for {date_str}.

Below are today's top-scored items from tech sources (HN, arXiv, GitHub, RSS, YouTube),
already scored by relevance to investment opportunities.{trends_block}

ITEMS:
{chr(10).join(items_block)}

Generate a digest in this exact JSON format:
{{
  "headline": "A compelling one-line headline summarizing today's most important signal (max 100 chars)",
  "summary": "2-3 sentence executive summary of the day's key themes and signals",
  "top_items_commentary": [
    {{"title": "item title", "score": 85, "source": "HN", "url": "...", "commentary": "One sentence on why this matters for investors"}}
  ],
  "trend_section": "If there are trend alerts, 2-3 sentences interpreting them. Otherwise empty string.",
  "one_line_takeaway": "The single most actionable insight from today"
}}

Return ONLY valid JSON."""

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
            "headline": f"Signal Digest — {date_str}",
            "summary": f"Collected {len(top_items)} items. LLM digest generation failed.",
            "top_items_commentary": [],
            "trend_section": "",
            "one_line_takeaway": "Review items manually.",
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
        parts.extend(["", "## Trend Alerts", data["trend_section"]])

    if data.get("one_line_takeaway"):
        parts.extend(["", f"**Takeaway:** {data['one_line_takeaway']}"])

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
