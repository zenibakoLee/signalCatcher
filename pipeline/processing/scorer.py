from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from pipeline import llm
from pipeline.db import get_connection

logger = logging.getLogger(__name__)

CONFIG_DIR = Path(__file__).resolve().parent.parent.parent / "config"
BATCH_SIZE = 100
MAX_SNIPPET_CHARS = 2_000
SCORING_MAX_OUTPUT_TOKENS = 20_000
MAX_SCORED_ITEMS_PER_RUN = 400
MODEL = llm.MODEL


def _load_scoring_prompt() -> str:
    return (CONFIG_DIR / "scoring_prompt.txt").read_text()


def score_items(item_ids: list[int], *, run_id: str | int) -> int:
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
    to_score = [item_id for item_id in item_ids if item_id not in already_scored]
    if len(to_score) > MAX_SCORED_ITEMS_PER_RUN:
        raise llm.RequestLimitExceededError(
            f"scoring run has {len(to_score)} items; cap is {MAX_SCORED_ITEMS_PER_RUN}"
        )
    if not to_score:
        logger.info("Scorer: all %d items already scored", len(item_ids))
        return 0
    rows = conn.execute(
        f"""SELECT id, source, title, url, content_snippet, metadata
            FROM raw_items WHERE id IN ({','.join('?' for _ in to_score)})""",
        to_score,
    ).fetchall()
    system_prompt = _load_scoring_prompt()
    staged: list[tuple[Any, dict]] = []
    for batch_start in range(0, len(rows), BATCH_SIZE):
        batch = rows[batch_start : batch_start + BATCH_SIZE]
        staged.extend(_score_batch(system_prompt, batch, conn=conn, run_id=run_id))
    scored_count = 0
    try:
        conn.execute("BEGIN")
        for row, score_entry in staged:
            score = _apply_source_penalty(row, score_entry["score"])
            tickers = score_entry["related_tickers"]
            cursor = conn.execute(
                """INSERT OR IGNORE INTO scored_items
                   (raw_item_id, score, score_reasoning, category, title_ko, related_tickers, model_used)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (row["id"], score, score_entry["reasoning"], score_entry["category"],
                 score_entry["title_ko"], json.dumps(tickers, ensure_ascii=False) if tickers else None, MODEL),
            )
            if cursor.rowcount != 1:
                raise llm.LLMOutputError("scorer insert did not insert exactly one staged row")
            scored_count += 1
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    logger.info("Scorer: %d items scored", scored_count)
    return scored_count


def _format_buzz(metadata_json: str | None) -> str:
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
    system_prompt: str, batch: list[Any], *, conn=None, run_id: str | int
) -> list[tuple[Any, dict]]:
    items_text = []
    for index, row in enumerate(batch, 1):
        snippet = (row["content_snippet"] or "")[:MAX_SNIPPET_CHARS]
        items_text.append(f'{index}. [{row["source"].upper()}]{_format_buzz(row["metadata"])} "{row["title"]}" — {snippet}')
    user_message = (
        "Score these items. Return ONLY a JSON object, no other text:\n\n"
        + "\n".join(items_text)
        + '\n\nReturn: {"scores": [{"index": 1, "score": 85, "reasoning": "...", "category": "...", "title_ko": "...", "related_tickers": ["NVDA", "삼성전자"]}, ...]}'
        + "\n\ntitle_ko: 기술 비전공자도 이해할 수 있게 번역하세요. 전문 용어 대신 쉬운 표현을 쓰세요. 고유명사(회사명, 제품명)는 원어 유지."
    )
    if conn is None:
        conn = get_connection()
    llm.require_no_business_transaction(conn)
    result = llm.get_boundary().complete(
        instructions=system_prompt,
        input_text=user_message,
        max_output_tokens=SCORING_MAX_OUTPUT_TOKENS,
        workload="scoring",
        run_id=run_id,
        output_schema=_score_schema(len(batch)),
    )
    scores = result.parsed["scores"]
    indices = [entry["index"] for entry in scores]
    if sorted(indices) != list(range(1, len(batch) + 1)):
        raise llm.LLMParseError("scoring response indices must be unique and complete")
    return [(batch[entry["index"] - 1], entry) for entry in scores]


def _apply_source_penalty(row: Any, score: int) -> int:
    if row["source"] != "youtube":
        return score
    try:
        meta = json.loads(row["metadata"] or "{}")
    except (json.JSONDecodeError, TypeError):
        return score
    return int(score * 0.75) if meta.get("search_query") else score


def _score_schema(batch_size: int) -> dict[str, Any]:
    score_entry = {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "minimum": 1, "maximum": batch_size},
            "score": {"type": "integer", "minimum": 0, "maximum": 100},
            "reasoning": {"type": "string", "minLength": 1, "maxLength": 2_000},
            "category": {
                "type": "string",
                "maxLength": 32,
                "enum": ["breakthrough", "trend", "product", "research", "infrastructure", "policy"],
            },
            "title_ko": {"type": "string", "minLength": 1, "maxLength": 500},
            "related_tickers": {
                "type": "array", "maxItems": 3,
                "items": {"type": "string", "minLength": 1, "maxLength": 32},
            },
        },
        "required": ["index", "score", "reasoning", "category", "title_ko", "related_tickers"],
        "additionalProperties": False,
    }
    return llm.strict_object_schema(
        "scoring_results",
        {"scores": {"type": "array", "minItems": batch_size, "maxItems": batch_size, "items": score_entry}},
    )
