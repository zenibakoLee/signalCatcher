from __future__ import annotations

import json
import logging
import os

import httpx

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

SAGE_GREEN = 0x5C7553
EMBER_ORANGE = 0xD4623A
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_TOTAL = 6000


def deliver_digest(digest_data: dict, date_str: str) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("Discord: DISCORD_WEBHOOK_URL not set, skipping delivery")
        return False

    embeds = _build_embeds(digest_data, date_str)
    return _send_embeds(webhook_url, embeds, date_str)


def _build_embeds(data: dict, date_str: str) -> list[dict]:
    headline = data.get("headline", f"Signal Digest — {date_str}")
    summary = data.get("summary", "")
    takeaway = data.get("one_line_takeaway", "")

    description = summary
    if takeaway:
        description += f"\n\n**Takeaway:** {takeaway}"
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 3] + "..."

    fields = []
    for item in data.get("top_items_commentary", [])[:10]:
        score = item.get("score", "?")
        source = item.get("source", "?")
        title = item.get("title", "Untitled")[:80]
        url = item.get("url", "")
        commentary = item.get("commentary", "")

        name = f"{'🔴' if isinstance(score, int) and score >= 90 else '🟡' if isinstance(score, int) and score >= 70 else '🟢'} [{score}] {source}"

        link = f"[{title}]({url})" if url else title
        value = f"{link}\n{commentary}"
        if len(value) > MAX_FIELD_VALUE:
            value = value[: MAX_FIELD_VALUE - 3] + "..."

        fields.append({"name": name, "value": value, "inline": False})

    main_embed = {
        "title": headline[:256],
        "description": description,
        "color": SAGE_GREEN,
        "fields": fields,
        "footer": {"text": f"Signal Catcher | {date_str}"},
    }

    embeds = [main_embed]

    trend_section = data.get("trend_section", "")
    if trend_section:
        trend_embed = {
            "title": "📈 Trend Alerts",
            "description": trend_section[:MAX_DESCRIPTION],
            "color": EMBER_ORANGE,
        }
        embeds.append(trend_embed)

    return embeds


def _send_embeds(webhook_url: str, embeds: list[dict], date_str: str) -> bool:
    total_chars = sum(
        len(e.get("title", ""))
        + len(e.get("description", ""))
        + sum(len(f.get("name", "")) + len(f.get("value", "")) for f in e.get("fields", []))
        for e in embeds
    )

    if total_chars > MAX_TOTAL:
        main = embeds[0]
        main["fields"] = main["fields"][:5]
        logger.warning("Discord: truncated to 5 fields to stay under 6000 char limit")

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": embeds},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()

        conn = get_connection()
        conn.execute(
            "UPDATE digests SET delivered = 1 WHERE digest_date = ?", (date_str,)
        )
        conn.commit()

        logger.info("Discord: digest delivered for %s", date_str)
        return True

    except Exception:
        logger.exception("Discord: failed to deliver digest")
        return False
