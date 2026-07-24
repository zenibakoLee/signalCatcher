from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import httpx

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

SAGE_GREEN = 0x5C7553
EMBER_ORANGE = 0xD4623A
LAVENDER = 0x7B68AE
MAX_DESCRIPTION = 4096
MAX_FIELD_VALUE = 1024
MAX_TOTAL = 6000

_URL_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "logs" / "tunnel-url.txt"


def _dashboard_url() -> str:
    try:
        return _URL_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def deliver_digest(digest_data: dict, date_str: str, comic_path: str | None = None) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        logger.warning("Discord: DISCORD_WEBHOOK_URL not set, skipping delivery")
        return False

    embeds = _build_embeds(digest_data, date_str)
    return _send_embeds(webhook_url, embeds, date_str, comic_path=comic_path)


def _build_embeds(data: dict, date_str: str) -> list[dict]:
    headline = data.get("headline", f"시그널 다이제스트 — {date_str}")
    summary = data.get("summary", "")
    takeaway = data.get("one_line_takeaway", "")

    description = summary
    if takeaway:
        description += f"\n\n**핵심 인사이트:** {takeaway}"
    dash = _dashboard_url()
    if dash:
        description += f"\n\n🔗 [대시보드 바로가기]({dash})"
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
        tickers = item.get("related_tickers") or []
        ticker_str = f"\n📌 관련종목: {', '.join(tickers)}" if tickers else ""
        value = f"{link}\n{commentary}{ticker_str}"
        if len(value) > MAX_FIELD_VALUE:
            value = value[: MAX_FIELD_VALUE - 3] + "..."

        fields.append({"name": name, "value": value, "inline": False})

    main_embed = {
        "title": headline[:256],
        "description": description,
        "color": SAGE_GREEN,
        "fields": fields,
        "footer": {"text": f"시그널 캐처 | {date_str} | 대시보드: {_dashboard_url()}"},
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


def _send_embeds(webhook_url: str, embeds: list[dict], date_str: str, comic_path: str | None = None) -> bool:
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
            if comic_path and os.path.exists(comic_path):
                import mimetypes
                name = os.path.basename(comic_path)
                mime = mimetypes.guess_type(comic_path)[0] or "image/png"
                with open(comic_path, "rb") as fh:
                    resp = client.post(
                        webhook_url,
                        data={"payload_json": json.dumps({"embeds": embeds})},
                        files={"file": (name, fh, mime)},
                    )
            else:
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
        "footer": {"text": f"시그널 캐처 | {conf['start_date']} ~ {conf['end_date']} | 대시보드: {_dashboard_url()}"},
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
        "footer": {"text": f"시그널 캐처 | 주간 이동평균 4주 연속 상승 | 대시보드: {_dashboard_url()}"},
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


def deliver_collector_errors(errors: list[str], date_str: str) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url or not errors:
        return False

    lines = []
    for err in errors[:10]:
        source = err.split(":")[0] if ":" in err else "unknown"
        lines.append(f"• **{source}** — {err}")

    embed = {
        "title": f"⚠️ 수집기 부분 오류 — {date_str}",
        "description": "\n".join(lines),
        "color": 0xFFA500,
        "footer": {"text": f"시그널 캐처 | 파이프라인은 정상 완료됨 | 대시보드: {_dashboard_url()}"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: collector error alert delivered (%d errors)", len(errors))
        return True
    except Exception:
        logger.exception("Discord: failed to deliver collector error alert")
        return False


def deliver_error_alert(pipeline_type: str, error_msg: str) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False

    embed = {
        "title": f"⚠️ 파이프라인 오류: {pipeline_type}",
        "description": f"```\n{error_msg[:3000]}\n```",
        "color": 0xFF0000,
        "footer": {"text": f"시그널 캐처 | 로그 확인: data/logs/pipeline.log | 대시보드: {_dashboard_url()}"},
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


COPPER = 0xD0A661


def deliver_investment_theses(theses: list[dict]) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url or not theses:
        return False

    market_read = theses[0].get("market_read", "")
    conv_icon = {"high": "🔥", "medium": "▪️", "low": "·"}
    flag = {"US": "🇺🇸", "KR": "🇰🇷", "JP": "🇯🇵"}

    def _fields(direction: str) -> list[dict]:
        out = []
        for t in [x for x in theses if x.get("direction") == direction][:6]:
            tk = f" ({t['ticker']})" if t.get("ticker") else ""
            name = f"{flag.get(t.get('market'), '')} {conv_icon.get(t.get('conviction'), '')} {t.get('company', '?')}{tk}"
            body = f"**{t.get('bottleneck', '')}**\n{t.get('reasoning', '')}"
            if t.get("falsifier"):
                body += f"\n↩︎ 반증: {t['falsifier']}"
            out.append({"name": name[:256], "value": body[:1024], "inline": False})
        return out

    embeds = []
    buy_fields = _fields("buy")
    avoid_fields = _fields("avoid")
    if buy_fields:
        embeds.append({
            "title": "🎯 투자 대상 발굴 (매수)",
            "description": market_read[:500],
            "color": SAGE_GREEN,
            "fields": buy_fields,
            "footer": {"text": "시그널 캐처 | 2차적 추론 · 확신도 🔥high ▪️mid ·low"},
        })
    if avoid_fields:
        embeds.append({
            "title": "🛑 회피·청산 후보 (과도기 프리미엄 회귀)",
            "color": EMBER_ORANGE,
            "fields": avoid_fields,
        })
    if not embeds:
        return False

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": embeds},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: investment theses delivered (%d)", len(theses))
        return True
    except Exception:
        logger.exception("Discord: failed to deliver investment theses")
        return False


def deliver_company_analyses(analyses: list[dict]) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url or not analyses:
        return False

    lines = []
    for a in analyses[:10]:
        ticker = a.get("ticker", "?")
        score = a.get("momentum_score", 0)
        verdict = a.get("verdict", "?")
        icon = "🔴" if score >= 70 else "🟡" if score >= 50 else "🟢"
        action = a.get("action_note", "")
        lines.append(f"{icon} **{ticker}** — {verdict} (모멘텀 {score}) · {action}")

    embed = {
        "title": f"📊 기업 모멘텀 분석 — {len(analyses)}건",
        "description": "\n".join(lines),
        "color": COPPER,
        "footer": {"text": f"시그널 캐처 | 상세 → {_dashboard_url()}/analyses"},
    }

    try:
        with httpx.Client(timeout=30) as client:
            resp = client.post(
                webhook_url,
                json={"embeds": [embed]},
                headers={"Content-Type": "application/json"},
            )
            resp.raise_for_status()
        logger.info("Discord: company analyses summary delivered (%d)", len(analyses))
        return True
    except Exception:
        logger.exception("Discord: failed to deliver company analyses")
        return False


def deliver_keyword_management(result: dict) -> bool:
    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        return False

    added = result.get("added", [])
    spiked = result.get("spiked", [])
    resurged = result.get("resurged", [])
    retired = result.get("retired", [])

    if not added and not spiked and not resurged and not retired:
        return False

    lines = []
    if added:
        kw_list = ", ".join(f"**{a['keyword']}** (`{a.get('category', '?')}`)" for a in added[:8])
        lines.append(f"➕ 신규 발견: {kw_list}")
    if spiked:
        kw_list = ", ".join(f"**{s['keyword']}** (오늘 {s['today_count']}회)" for s in spiked[:8])
        lines.append(f"📈 스파이크 추가: {kw_list}")
    if resurged:
        kw_list = ", ".join(f"**{kw}**" for kw in resurged[:8])
        lines.append(f"♻️ 재부상 (은퇴 → 재활성): {kw_list}")
    if retired:
        kw_list = ", ".join(f"~~{kw}~~" for kw in retired[:8])
        lines.append(f"🗑️ 은퇴: {kw_list}")

    embed = {
        "title": "🔄 키워드 자동 관리",
        "description": "\n\n".join(lines),
        "color": LAVENDER,
        "footer": {"text": f"시그널 캐처 | 매일 자동 실행 | 대시보드: {_dashboard_url()}"},
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
