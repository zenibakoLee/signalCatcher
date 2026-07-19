from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta, timezone

import anthropic

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

MODEL = "claude-haiku-4-5-20251001"

KST = timezone(timedelta(hours=9))


def _kst_date_to_utc_range(d: date) -> tuple[str, str]:
    """Convert a KST date to UTC start/end timestamps for DB queries."""
    kst_start = datetime(d.year, d.month, d.day, tzinfo=KST)
    utc_start = kst_start.astimezone(timezone.utc)
    utc_end = utc_start + timedelta(days=1)
    return utc_start.strftime("%Y-%m-%dT%H:%M:%S"), utc_end.strftime("%Y-%m-%dT%H:%M:%S")


_CLUSTER_STOPWORDS = {
    "the", "a", "an", "and", "or", "for", "with", "from", "how", "why", "what",
    "show", "ask", "hn", "using", "your", "you", "new", "not", "are", "can",
    "this", "that", "will", "just", "has", "have", "its", "into", "about",
    "ai", "model", "models", "llm", "gpt", "video", "part",
}


def _detect_topic_clusters(conn, hours: int = 72, min_count: int = 8) -> list[dict]:
    """단기간 다수 항목이 몰린 토픽 감지 — 개별 점수와 무관한 볼륨 신호.

    개별 항목이 중간 점수여도(예: Kimi K3 출시 직후 개별 60~70점),
    같은 토픽이 며칠 새 수십 건 수집되는 것 자체가 커뮤니티 파장의 직접 증거다.
    제목의 토큰/바이그램 빈도로 클러스터를 찾아 다이제스트가 반드시 다루게 한다.
    """
    from collections import Counter
    import re

    def _grams_of(title: str) -> set[str]:
        tokens = [t for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.\-]+", title.lower()) if t not in _CLUSTER_STOPWORDS]
        grams = set(t for t in tokens if len(t) >= 3)
        grams.update(f"{tokens[i]} {tokens[i+1]}" for i in range(len(tokens) - 1))
        return grams

    rows = conn.execute(
        """SELECT r.title, s.score FROM raw_items r
           LEFT JOIN scored_items s ON s.raw_item_id = r.id
           WHERE r.collected_at >= datetime('now', ?)""",
        (f"-{hours} hours",),
    ).fetchall()
    if not rows:
        return []

    gram_counts: Counter = Counter()
    gram_titles: dict[str, list] = {}
    for row in rows:
        for g in _grams_of(row["title"] or ""):
            gram_counts[g] += 1
            gram_titles.setdefault(g, []).append((row["score"] or 0, row["title"]))

    # baseline: 이전 14일 — "code", "claude" 같은 상시 배경 토픽을 걸러내고
    # Kimi K3처럼 갑자기 등장한 급증 토픽만 남긴다
    base_rows = conn.execute(
        """SELECT title FROM raw_items
           WHERE collected_at >= datetime('now', ?) AND collected_at < datetime('now', ?)""",
        (f"-{hours + 14 * 24} hours", f"-{hours} hours"),
    ).fetchall()
    base_counts: Counter = Counter()
    for row in base_rows:
        for g in _grams_of(row["title"] or ""):
            base_counts[g] += 1

    clusters = []
    used_grams: set[str] = set()
    for gram, cnt in gram_counts.most_common(50):
        if cnt < min_count:
            break
        # 기대치 = baseline 일평균 × 수집 시간 — 4배 이상 급증만 클러스터로 인정
        expected = base_counts.get(gram, 0) / 14 * (hours / 24)
        surge = cnt / max(expected, 1.0)
        if surge < 4:
            continue
        if any(gram in u or u in gram for u in used_grams):
            continue
        used_grams.add(gram)
        titles = sorted(gram_titles[gram], reverse=True)
        clusters.append({
            "topic": gram,
            "count": cnt,
            "surge_ratio": round(surge, 1),
            "max_score": titles[0][0],
            "sample_titles": [t for _, t in titles[:3]],
        })
        if len(clusters) >= 3:
            break
    return clusters


def generate_digest(target_date: date | None = None) -> dict | None:
    if target_date is None:
        target_date = datetime.now(KST).date()
    date_str = target_date.isoformat()

    conn = get_connection()

    existing = conn.execute(
        "SELECT id FROM digests WHERE digest_date = ?", (date_str,)
    ).fetchone()
    if existing:
        logger.info("Digest: already exists for %s", date_str)
        return None

    utc_start, utc_end = _kst_date_to_utc_range(target_date)

    top_items = conn.execute(
        """SELECT s.raw_item_id, s.score, s.score_reasoning, s.category,
                  r.title, r.url, r.source, r.content_snippet
           FROM scored_items s
           JOIN raw_items r ON s.raw_item_id = r.id
           WHERE r.collected_at >= ? AND r.collected_at < ?
           ORDER BY s.score DESC
           LIMIT 15""",
        (utc_start, utc_end),
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

    cluster_block = ""
    try:
        clusters = _detect_topic_clusters(conn)
        if clusters:
            lines = []
            for c in clusters:
                lines.append(
                    f'- "{c["topic"]}": 최근 72시간 {c["count"]}건 집중 수집, 평소 대비 {c.get("surge_ratio", "?")}배 급증 (개별 최고점 {c["max_score"]}) '
                    f'— 예: {c["sample_titles"][0][:60]}'
                )
            cluster_block = (
                "\n\n🔥 집중 화제 클러스터 (볼륨 신호 — 개별 점수와 무관하게 커뮤니티 파장의 직접 증거):\n"
                + "\n".join(lines)
                + "\n위 클러스터 토픽은 개별 항목 점수가 중간이어도 반드시 headline 또는 summary에서 다루고, "
                "왜 이렇게 화제인지와 투자 함의를 설명하세요."
            )
    except Exception:
        logger.exception("Cluster detection failed")

    buzz_block = ""
    try:
        from pipeline.collectors.apewisdom import get_ai_buzz_summary
        buzz = get_ai_buzz_summary(target_date)
        if buzz:
            buzz_lines = [f'- {b["ticker"]}: {b["mentions"]}회 멘션, {b["upvotes"]} 업보트 (순위 #{b["rank"]})' for b in buzz[:10]]
            buzz_block = "\n\nREDDIT SOCIAL BUZZ (AI 관련 종목):\n" + "\n".join(buzz_lines)
    except Exception:
        logger.debug("Social buzz data not available for digest")

    prompt = f"""당신은 개인 투자자를 위한 기술 시그널 분석가입니다.
대상 독자는 기술 비전공자인 개인 투자자입니다. 전문 용어를 쓰지 말고 쉽게 설명하세요.
모든 출력은 반드시 한국어로 작성하세요.

아래는 {date_str} 기술 소스(HN, arXiv, GitHub, RSS, YouTube)에서 수집된 상위 항목입니다.
투자 기회 관련도에 따라 이미 점수가 매겨져 있습니다.{trends_block}{cluster_block}{buzz_block}

항목:
{chr(10).join(items_block)}

아래 JSON 형식으로 다이제스트를 생성하세요:
{{
  "headline": "오늘 가장 투자 임팩트가 큰 단일 시그널을 구체적으로 지목하는 한 줄 (최대 80자, 한국어). 기술 전문 용어 없이 누구나 이해할 수 있게. 예: 'Cerebras $5.5B IPO 성공 — AI 전용 칩 시장 본격 개화'",
  "summary": "오늘 수집된 시그널 중 투자 판단에 직접 영향을 주는 2-3개를 각각 한 문장으로 설명 (한국어). 전문 용어 대신 '이 기술이 왜 돈이 되는지' 관점으로 쉽게 설명.",
  "top_items_commentary": [
    {{"title": "항목 제목 (원문 유지)", "score": 85, "source": "HN", "url": "...", "commentary": "기술 비전공 투자자가 바로 이해할 수 있게 왜 중요한지 한 문장 설명 (한국어)", "related_tickers": ["NVDA", "삼성전자"]}}
  ],
  "trend_section": "트렌드 알림이 있으면 2-3문장으로 쉽게 해석 (한국어). 없으면 빈 문자열.",
  "social_buzz_note": "Reddit 소셜 버즈 데이터가 있으면 주목할 멘션 급등/급락 한 문장 (한국어). 없으면 빈 문자열.",
  "one_line_takeaway": "오늘의 핵심 인사이트 한 줄 (한국어). 구체적 종목/섹터를 지목하고, 왜 유망한지 한마디 덧붙일 것."
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

    if data.get("social_buzz_note"):
        parts.extend(["", "## 소셜 버즈", data["social_buzz_note"]])

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
