from __future__ import annotations

import json
import logging
from pathlib import Path

import anthropic

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
BATCH_SIZE = 10
MODEL = "claude-haiku-4-5-20251001"


def _load_scoring_prompt() -> str:
    return (CONFIG_DIR / "scoring_prompt.txt").read_text()


def score_items(item_ids: list[int]) -> int:
    if not item_ids:
        return 0

    conn = get_connection()
    already_scored = {
        r["raw_item_id"]
        for r in conn.execute(
            f"SELECT raw_item_id FROM scored_items WHERE raw_item_id IN ({','.join('?' for _ in item_ids)})",
            item_ids,
        ).fetchall()
    }
    to_score = [i for i in item_ids if i not in already_scored]
    if not to_score:
        logger.info("Scorer: all %d items already scored", len(item_ids))
        return 0

    rows = conn.execute(
        f"""SELECT id, source, title, url, content_snippet, metadata
            FROM raw_items WHERE id IN ({','.join('?' for _ in to_score)})""",
        to_score,
    ).fetchall()

    system_prompt = _load_scoring_prompt()
    client = anthropic.Anthropic()
    scored_count = 0

    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        scored_count += _score_batch(conn, client, system_prompt, batch)

    logger.info("Scorer: %d items scored", scored_count)
    return scored_count


def _format_buzz(metadata_json: str | None) -> str:
    """Community engagement markers for the scoring prompt (buzz = 선행 신호)."""
    try:
        meta = json.loads(metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        return ""
    parts = []
    if meta.get("points"):
        parts.append(f"HN {meta['points']}pts/{meta.get('num_comments', 0)}cmt")
    if meta.get("view_count"):
        parts.append(f"조회수 {meta['view_count']:,}")
    return f" ({', '.join(parts)})" if parts else ""


def _score_batch(
    conn,
    client: anthropic.Anthropic,
    system_prompt: str,
    batch: list,
) -> int:
    items_text = []
    for i, row in enumerate(batch, 1):
        snippet = (row["content_snippet"] or "")[:2000]
        buzz = _format_buzz(row["metadata"])
        items_text.append(f'{i}. [{row["source"].upper()}]{buzz} "{row["title"]}" — {snippet}')

    user_message = (
        "Score these items. Return ONLY a JSON array, no other text:\n\n"
        + "\n".join(items_text)
        + '\n\nReturn: [{"index": 1, "score": 85, "reasoning": "...", "category": "...", "title_ko": "...", "related_tickers": ["NVDA", "삼성전자"]}, ...]'
        + "\n\ntitle_ko: 기술 비전공자도 이해할 수 있게 번역하세요. 전문 용어 대신 쉬운 표현을 쓰세요. 고유명사(회사명, 제품명)는 원어 유지."
        + "\n예: 'KV Cache Editability' → 'AI 모델 운영 비용을 줄이는 새 기술'"
    )

    try:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4000,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        text = response.content[0].text.strip()
        scores = _parse_scores(text)
        if not scores:
            raise ValueError("Empty scores from LLM response")
    except Exception:
        logger.exception("Scorer: LLM call failed for batch, using fallback scores")
        scores = [
            {"index": i, "score": 50, "reasoning": "Auto-scored: LLM unavailable", "category": "trend", "title_ko": None, "related_tickers": []}
            for i in range(1, len(batch) + 1)
        ]

    count = 0
    for score_entry in scores:
        idx = score_entry.get("index", 0) - 1
        if idx < 0 or idx >= len(batch):
            continue

        row = batch[idx]
        score = max(0, min(100, score_entry.get("score", 50)))
        score = _apply_source_penalty(row, score)
        tickers = score_entry.get("related_tickers") or []
        tickers_json = json.dumps(tickers, ensure_ascii=False) if tickers else None
        try:
            conn.execute(
                """INSERT OR IGNORE INTO scored_items
                   (raw_item_id, score, score_reasoning, category, title_ko, related_tickers, model_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["id"],
                    score,
                    score_entry.get("reasoning", ""),
                    score_entry.get("category", "trend"),
                    score_entry.get("title_ko"),
                    tickers_json,
                    MODEL,
                ),
            )
            count += 1
        except Exception:
            logger.exception("Scorer: failed to insert score for item %d", row["id"])

    conn.commit()
    return count


def _apply_source_penalty(row, score: int) -> int:
    if row["source"] != "youtube":
        return score
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return score
    if meta.get("search_query"):
        score = int(score * 0.75)
    return score


def _parse_scores(text: str) -> list[dict]:
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start != -1 and end != -1:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("Scorer: could not parse LLM response as JSON")
        return []
