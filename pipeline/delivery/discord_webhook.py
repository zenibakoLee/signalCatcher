from __future__ import annotations

import json
import logging
import os

import httpx

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

SAGE_GREEN = 0x5C7553
EMBER_ORANGE = 0xD4623A
LAVENDER = 0x7B68AE
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
    headline = data.get("headline", f"시그널 다이제스트 — {date_str}")
    summary = data.get("summary", "")
    takeaway = data.get("one_line_takeaway", "")

    description = summary
    if takeaway:
        description += f"\n\n**핵심 인사이트:** {takeaway}"
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
        "footer": {"text": f"시그널 캐처 | {date_str}"},
    }

    embeds = [main_embed]

    trend_section = data.get("trend_section", "")
    if trend_section:
        trend_embed = {
            "title": "📈 트렌드 알림",
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


DEEP_BLUE = 0x1E3A5F


def deliver_conference_briefing(data: dict, conf: dict, briefing_type: str) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("Discord: DISCORD_WEBHOOK_URL not set, skipping delivery")
        return False

    is_pre = briefing_type == "pre_event"
    title_prefix = "📋 사전 브리핑" if is_pre else "📊 사후 브리핑"
    title = f"{title_prefix}: {conf['name']}"
    headline = data.get("headline", title)

    description = data.get("summary", "")
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 3] + "..."

    fields = []

    if is_pre:
        items_text = ""
        for item in data.get("expected_items", [])[:8]:
            rel = item.get("investment_relevance", "?")
            icon = "🔴" if rel == "높음" else "🟡" if rel == "중간" else "🟢"
            items_text += f"{icon} **{item['item']}** ({rel})\n"
        if items_text:
            fields.append({"name": "예상 발표 항목", "value": items_text[:MAX_FIELD_VALUE], "inline": False})
        if data.get("watch_points"):
            fields.append({"name": "투자자 주목 포인트", "value": data["watch_points"][:MAX_FIELD_VALUE], "inline": False})
    else:
        announcements = ""
        for a in data.get("key_announcements", [])[:5]:
            announcements += f"• **{a['item']}** — {a.get('significance', '')}\n"
        if announcements:
            fields.append({"name": "핵심 발표", "value": announcements[:MAX_FIELD_VALUE], "inline": False})

        silent = ""
        for s in data.get("silent_signals", [])[:5]:
            silent += f"🔇 **{s['expected_item']}** — {s.get('interpretation', '')}\n"
        if silent:
            fields.append({"name": "Silent Signals", "value": silent[:MAX_FIELD_VALUE], "inline": False})

        surprises = ""
        for s in data.get("surprises", [])[:3]:
            surprises += f"⚡ **{s['item']}** — {s.get('significance', '')}\n"
        if surprises:
            fields.append({"name": "서프라이즈", "value": surprises[:MAX_FIELD_VALUE], "inline": False})

        if data.get("investment_takeaway"):
            fields.append({"name": "투자 시사점", "value": data["investment_takeaway"][:MAX_FIELD_VALUE], "inline": False})

    embed = {
        "title": headline[:256],
        "description": description,
        "color": DEEP_BLUE,
        "fields": fields,
        "footer": {"text": f"시그널 캐처 | {conf['start_date']} ~ {conf['end_date']}"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()

        conn = get_connection()
        conn.execute(
            "UPDATE conference_briefings SET delivered = 1 WHERE conference_name = ? AND conference_start = ? AND briefing_type = ?",
            (conf["name"], conf["start_date"], briefing_type),
        )
        conn.commit()

        logger.info("Discord: conference %s briefing delivered for %s", briefing_type, conf["name"])
        return True
    except Exception:
        logger.exception("Discord: failed to deliver conference briefing")
        return False




def deliver_keyword_suggestions(suggestions: list[dict]) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("Discord: DISCORD_WEBHOOK_URL not set, skipping delivery")
        return False

    lines = []
    for s in suggestions[:10]:
        cat = s.get("category", "?")
        lines.append(f"**{s['keyword']}** (`{cat}`)\n{s.get('reason', '')}")

    description = "\n\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 3] + "..."

    embed = {
        "title": "🔍 주간 키워드 제안",
        "description": description,
        "color": LAVENDER,
        "footer": {"text": "시그널 캐처 | 대시보드 설정에서 승인/거부"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: keyword suggestions delivered (%d items)", len(suggestions))
        return True
    except Exception:
        logger.exception("Discord: failed to deliver keyword suggestions")
        return False


def deliver_acceleration_alerts(alerts: list) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url or not alerts:
        return False

    lines = []
    for a in alerts[:10]:
        growth_pct = round(a.z_score * 100)
        lines.append(
            f"**{a.keyword}** — +{growth_pct}% 성장 (4주 연속 상승)\n"
            f"최근 주간 평균 {a.moving_avg_7d}회 · 전체 평균 {a.moving_avg_30d}회"
        )

    description = "\n\n".join(lines)
    if len(description) > MAX_DESCRIPTION:
        description = description[: MAX_DESCRIPTION - 3] + "..."

    embed = {
        "title": "📊 장기 가속 감지",
        "description": description,
        "color": SAGE_GREEN,
        "footer": {"text": "시그널 캐처 | 주간 이동평균 4주 연속 상승"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: acceleration alerts delivered (%d items)", len(alerts))
        return True
    except Exception:
        logger.exception("Discord: failed to deliver acceleration alerts")
        return False


def deliver_error_alert(pipeline_type: str, error_msg: str) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False

    embed = {
        "title": f"⚠️ 파이프라인 오류: {pipeline_type}",
        "description": f"```\n{error_msg[:3000]}\n```",
        "color": 0xFF0000,
        "footer": {"text": "시그널 캐처 | 로그 확인: data/logs/pipeline.log"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        return True
    except Exception:
        return False


def deliver_keyword_management(result: dict) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False

    promoted = result.get("promoted", 0)
    retired = result.get("retired", 0)
    auto_added = result.get("auto_added", 0)
    suggested = result.get("suggested", 0)

    if promoted == 0 and retired == 0 and auto_added == 0:
        return False

    lines = []
    if promoted:
        lines.append(f"**승격** {promoted}개 키워드 (suggested → active)")
    if auto_added:
        lines.append(f"**자동 추가** {auto_added}개 키워드 (고빈도 신규)")
    if retired:
        lines.append(f"**은퇴** {retired}개 키워드 (30일 무언급)")
    if suggested:
        lines.append(f"**제안** {suggested}개 후보 대기 중")

    embed = {
        "title": "🔄 키워드 자동 관리",
        "description": "\n".join(lines),
        "color": LAVENDER,
        "footer": {"text": "시그널 캐처 | 매일 자동 실행"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: keyword management report delivered")
        return True
    except Exception:
        logger.exception("Discord: failed to deliver keyword management report")
        return False
