from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote_plus

import anthropic
import httpx

from pipeline.db import get_connection


def _price_context(ticker: str) -> str:
    """Real-time quote + 5-day move from Toss Securities (empty string on failure)."""
    try:
        from pipeline.utils import toss_api
        if not toss_api.available():
            return ""
        quote = toss_api.get_prices([ticker]).get(ticker)
        if not quote:
            return ""
        line = f"현재가: {quote['price']:,.2f} {quote['currency']} (토스증권, {quote['timestamp'][:10]})"
        candles = toss_api.get_candles(ticker, interval="1d", count=6)
        closes = [float(c.get("closePrice", 0)) for c in candles if c.get("closePrice")]
        if len(closes) >= 2 and closes[0]:
            # Toss returns newest-first; compare oldest in window vs latest
            change = (closes[0] / closes[-1] - 1) * 100
            line += f", 최근 5거래일 {change:+.1f}%"
        return f"\n## 시장 데이터\n{line}\n"
    except Exception:
        logger.exception("price context failed for %s", ticker)
        return ""

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
KST = timezone(timedelta(hours=9))
WINDOW_DAYS = 30
MIN_SIGNALS = 3
MAX_ANALYSES_PER_RUN = 5

TICKER_ALIASES: dict[str, str] = {
    "GOOG": "GOOGL",
    "GOOGLE": "GOOGL",
    "ALPHABET": "GOOGL",
    "FB": "META",
    "FACEBOOK": "META",
    "ANTH": "ANTHROPIC",
    "TSM": "TSMC",
}


def find_momentum_candidates(
    window_days: int = WINDOW_DAYS,
    min_signals: int = MIN_SIGNALS,
) -> list[dict]:
    conn = get_connection()
    cutoff = (datetime.now(KST) - timedelta(days=window_days)).strftime("%Y-%m-%dT%H:%M:%S")
    today = datetime.now(KST).date().isoformat()

    already = {
        r["ticker"]
        for r in conn.execute(
            "SELECT ticker FROM company_analyses WHERE generated_at >= ?",
            (cutoff,),
        ).fetchall()
    }

    rows = conn.execute(
        """
        SELECT s.related_tickers, s.score, s.score_reasoning, s.category,
               r.title, r.url, r.source, r.content_snippet, r.published_at
        FROM scored_items s
        JOIN raw_items r ON s.raw_item_id = r.id
        WHERE s.related_tickers IS NOT NULL
          AND s.related_tickers != '[]'
          AND r.collected_at >= ?
        ORDER BY r.published_at DESC
        """,
        (cutoff,),
    ).fetchall()

    ticker_signals: dict[str, list[dict]] = {}
    for row in rows:
        try:
            tickers = json.loads(row["related_tickers"])
        except (json.JSONDecodeError, TypeError):
            continue
        for ticker in tickers:
            ticker = ticker.strip().upper()
            if not ticker:
                continue
            ticker = TICKER_ALIASES.get(ticker, ticker)
            ticker_signals.setdefault(ticker, []).append({
                "title": row["title"],
                "url": row["url"],
                "source": row["source"],
                "score": row["score"],
                "category": row["category"],
                "reasoning": row["score_reasoning"],
                "snippet": (row["content_snippet"] or "")[:300],
                "published_at": row["published_at"],
            })

    buzz = conn.execute(
        """
        SELECT ticker, SUM(mentions) as total_mentions, SUM(upvotes) as total_upvotes
        FROM social_buzz
        WHERE collected_date >= ?
        GROUP BY ticker
        HAVING total_mentions >= 10
        """,
        ((datetime.now(KST) - timedelta(days=7)).strftime("%Y-%m-%d"),),
    ).fetchall()
    buzz_map = {r["ticker"]: {"mentions": r["total_mentions"], "upvotes": r["total_upvotes"]} for r in buzz}

    candidates = []
    for ticker, signals in ticker_signals.items():
        if ticker in already:
            continue
        if len(signals) < min_signals:
            continue

        avg_score = sum(s["score"] for s in signals) / len(signals)
        high_score_count = sum(1 for s in signals if s["score"] >= 70)
        buzz_data = buzz_map.get(ticker, {})
        buzz_mentions = buzz_data.get("mentions", 0)

        priority = (
            len(signals) * 2
            + avg_score * 0.5
            + high_score_count * 10
            + min(buzz_mentions, 100) * 0.3
        )

        candidates.append({
            "ticker": ticker,
            "signal_count": len(signals),
            "avg_score": round(avg_score, 1),
            "high_score_count": high_score_count,
            "buzz_mentions": buzz_mentions,
            "priority": round(priority, 1),
            "signals": sorted(signals, key=lambda s: s["published_at"], reverse=True),
        })

    candidates.sort(key=lambda c: c["priority"], reverse=True)
    return candidates[:MAX_ANALYSES_PER_RUN]


_KNOWN_NAMES: dict[str, str] = {
    "NVDA": "NVIDIA",
    "MSFT": "Microsoft",
    "GOOGL": "Google Alphabet",
    "GOOG": "Google Alphabet",
    "AMZN": "Amazon",
    "AAPL": "Apple",
    "META": "Meta Platforms",
    "TSLA": "Tesla",
    "AMD": "AMD",
    "INTC": "Intel",
    "TSM": "TSMC",
    "TSMC": "TSMC",
    "MU": "Micron Technology",
    "QCOM": "Qualcomm",
    "ASML": "ASML",
    "삼성전자": "삼성전자 Samsung",
    "SK하이닉스": "SK하이닉스 SK Hynix",
}


def _fetch_news(ticker: str, max_results: int = 15) -> list[dict]:
    name = _KNOWN_NAMES.get(ticker, ticker)
    queries = [
        f"{name} stock earnings strategy 2026",
        f"{name} investment analysis competitive moat",
    ]
    if any(c > '　' for c in ticker):
        queries.append(f"{ticker} 실적 전략 투자 2026")

    articles: list[dict] = []
    seen_titles: set[str] = set()

    with httpx.Client(timeout=15, follow_redirects=True) as client:
        for query in queries:
            try:
                url = f"https://news.google.com/rss/search?q={quote_plus(query)}&hl=en-US&gl=US&ceid=US:en"
                resp = client.get(url, headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                root = ET.fromstring(resp.text)

                for item in root.iter("item"):
                    title_el = item.find("title")
                    link_el = item.find("link")
                    pubdate_el = item.find("pubDate")
                    desc_el = item.find("description")
                    if title_el is None or title_el.text is None:
                        continue

                    title = unescape(title_el.text.strip())
                    if title in seen_titles:
                        continue
                    seen_titles.add(title)

                    snippet = ""
                    if desc_el is not None and desc_el.text:
                        raw = unescape(desc_el.text)
                        import re
                        snippet = re.sub(r"<[^>]+>", "", raw)[:300]

                    articles.append({
                        "title": title,
                        "url": link_el.text.strip() if link_el is not None and link_el.text else "",
                        "date": pubdate_el.text.strip()[:16] if pubdate_el is not None and pubdate_el.text else "",
                        "snippet": snippet,
                    })

                    if len(articles) >= max_results:
                        break
            except Exception:
                logger.debug("News fetch failed for query: %s", query)

            if len(articles) >= max_results:
                break

    logger.info("Web search: %d articles fetched for %s", len(articles), ticker)
    return articles[:max_results]


def generate_analysis(candidate: dict) -> dict | None:
    ticker = candidate["ticker"]
    signals = candidate["signals"]

    web_articles = _fetch_news(ticker)
    web_block = ""
    if web_articles:
        web_lines = []
        for i, art in enumerate(web_articles, 1):
            web_lines.append(
                f'{i}. [{art["date"]}] {art["title"]}\n'
                f'   {art["snippet"]}'
            )
        web_block = "\n\n## 웹 검색 — 최신 뉴스/실적/전략 기사\n" + "\n".join(web_lines)

    signals_block = []
    for i, sig in enumerate(signals[:20], 1):
        signals_block.append(
            f'{i}. [{sig["source"].upper()} | Score: {sig["score"]}] {sig["title"]}\n'
            f'   날짜: {sig["published_at"][:10]} | 카테고리: {sig["category"]}\n'
            f'   분석: {sig["reasoning"]}\n'
            f'   발췌: {sig["snippet"]}'
        )

    if signals:
        source_intro = f"아래는 최근 {WINDOW_DAYS}일간 **{ticker}** 관련 수집된 시그널 {len(signals)}개와, 웹에서 추가 수집한 최신 기사입니다."
        source_section = f"## 수집된 시그널\n{chr(10).join(signals_block)}{web_block}"
    else:
        source_intro = f"**{ticker}**에 대한 기존 수집 시그널은 없습니다. 웹에서 수집한 최신 기사만을 기반으로 분석합니다."
        source_section = web_block.lstrip("\n") if web_block else "웹 검색 결과 없음"

    prompt = f"""당신은 기술 투자 시그널 분석 전문가입니다.
{source_intro}

이 자료들을 종합하여, 아래 5대 질문 프레임워크를 적용해 기업 모멘텀 분석 레포트를 생성하세요.

## 5대 질문 프레임워크
1. 돈 안 되는데 진심인 투자가 있는가? — 단기 수익이 아닌 장기 기술/인프라 투자 신호
2. 경쟁자가 물러나거나 못 따라오는가? — 경쟁 구도 변화 신호
3. 수요 곡선의 상한이 열려 있는가? — 시장 규모/성장 천장 제거 신호
4. 공급 병목이 가격 결정력을 주는가? — 공급 부족, 대기 수요 신호
5. 기술 검증과 주가 반영 사이에 시차가 있는가? — 아직 가격에 반영 안 된 기술적 진전

## 추가 거르기 질문
- 이 모멘텀은 테마(기대)인가, 청구서(실제 매출/계약)인가?

{source_section}

## 소셜 버즈
Reddit 멘션: {candidate.get('buzz_mentions', 0)}회 (최근 7일)
{_price_context(ticker)}

아래 JSON 형식으로 반환하세요:
{{
  "company_name": "회사명 (한글 또는 영문)",
  "market": "US" 또는 "KR",
  "momentum_score": 0~100 (현재 모멘텀 강도. 70+ = 강한 모멘텀, 50~69 = 관심, 50 미만 = 약함),
  "verdict": "강한 모멘텀" | "관심 관찰" | "모멘텀 약화" | "경고",
  "verdict_summary": "2~3문장. 이 기업의 현재 모멘텀을 비전공자도 이해할 수 있게 요약. 왜 주목할만한지, 또는 왜 주의해야 하는지.",
  "five_questions": [
    {{
      "question": "질문 원문",
      "answer": "yes" | "partial" | "no" | "unknown",
      "evidence": "시그널에서 발견된 근거 1~2문장 (한국어)"
    }}
  ],
  "fake_or_real": {{
    "judgment": "real" | "fake" | "mixed" | "too_early",
    "reasoning": "테마인가 청구서인가에 대한 판단 1~2문장 (한국어)"
  }},
  "signal_timeline": [
    {{
      "date": "YYYY-MM-DD",
      "event": "핵심 이벤트 한 줄 (한국어)",
      "significance": "왜 중요한지 한 줄 (한국어)"
    }}
  ],
  "risk_factors": [
    {{
      "factor": "리스크 요인 (한국어)",
      "severity": "high" | "medium" | "low"
    }}
  ],
  "action_note": "투자자를 위한 관전 포인트 한 줄 (한국어)"
}}

유효한 JSON만 반환하세요. 한국어로 작성하되 고유명사는 원어 유지."""

    try:
        client = anthropic.Anthropic()
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = response.content[0].text.strip()
        data = _parse_json(text)
    except Exception:
        logger.exception("Company analysis failed for %s", ticker)
        return None

    conn = get_connection()
    try:
        conn.execute(
            """INSERT INTO company_analyses
               (ticker, company_name, market, signal_count, signal_window_days,
                momentum_score, verdict, verdict_summary, five_questions,
                signal_timeline, risk_factors, key_signals_json, model_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                ticker,
                data.get("company_name", ticker),
                data.get("market", "US"),
                len(signals),
                WINDOW_DAYS,
                max(0, min(100, data.get("momentum_score", 50))),
                data.get("verdict", "관심 관찰"),
                data.get("verdict_summary", ""),
                json.dumps(data.get("five_questions", []), ensure_ascii=False),
                json.dumps(data.get("signal_timeline", []), ensure_ascii=False),
                json.dumps(data.get("risk_factors", []), ensure_ascii=False),
                json.dumps({
                    "signals_used": len(signals),
                    "avg_score": candidate["avg_score"],
                    "fake_or_real": data.get("fake_or_real", {}),
                    "action_note": data.get("action_note", ""),
                    "top_signals": [
                        {"title": s["title"], "source": s["source"], "score": s["score"], "url": s["url"]}
                        for s in signals[:10]
                    ],
                    "web_articles": [
                        {"title": a["title"], "url": a["url"], "date": a["date"]}
                        for a in web_articles[:8]
                    ],
                }, ensure_ascii=False),
                MODEL,
            ),
        )
        conn.commit()
        logger.info("Company analysis saved for %s (momentum=%d)", ticker, data.get("momentum_score", 0))
    except Exception:
        logger.exception("Failed to save analysis for %s", ticker)
        conn.rollback()
        return None

    return data


def run_company_analyses() -> list[dict]:
    candidates = find_momentum_candidates()
    if not candidates:
        logger.info("Company analysis: no candidates found")
        return []

    logger.info("Company analysis: %d candidates found", len(candidates))
    results = []
    for cand in candidates:
        logger.info(
            "Analyzing %s (%d signals, avg_score=%.1f, buzz=%d)",
            cand["ticker"], cand["signal_count"], cand["avg_score"], cand["buzz_mentions"],
        )
        data = generate_analysis(cand)
        if data:
            results.append({"ticker": cand["ticker"], **data})
    return results


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
