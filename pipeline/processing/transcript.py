from __future__ import annotations

import logging
import re
import subprocess
import tempfile
from pathlib import Path

from pipeline.db import get_connection

logger = logging.getLogger(__name__)

YT_DLP = "/Users/haejoonlee/.pyenv/shims/yt-dlp"
MAX_TRANSCRIPT_CHARS = 4000
BATCH_LIMIT = 30


def _extract_captions(video_id: str) -> str | None:
    url = f"https://www.youtube.com/watch?v={video_id}"
    with tempfile.TemporaryDirectory() as tmpdir:
        out_path = Path(tmpdir) / "sub"
        try:
            subprocess.run(
                [
                    YT_DLP,
                    "--skip-download",
                    "--write-auto-sub",
                    "--sub-lang", "en,ko",
                    "--sub-format", "vtt",
                    "--output", str(out_path),
                    url,
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("yt-dlp failed or timed out for %s", video_id)
            return None

        for suffix in [".en.vtt", ".ko.vtt"]:
            vtt_file = Path(str(out_path) + suffix)
            if vtt_file.exists():
                return _parse_vtt(vtt_file.read_text())

    return None


def _parse_vtt(vtt_text: str) -> str:
    lines = []
    for line in vtt_text.splitlines():
        if not line.strip():
            continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"):
            continue
        if re.match(r"^\d{2}:\d{2}", line):
            continue
        if line.startswith("NOTE"):
            continue
        cleaned = re.sub(r"<[^>]+>", "", line).strip()
        if cleaned and (not lines or lines[-1] != cleaned):
            lines.append(cleaned)
    return " ".join(lines)


def enrich_youtube_transcripts(item_ids: list[int]) -> int:
    if not item_ids:
        return 0

    conn = get_connection()
    rows = conn.execute(
        f"""SELECT id, source, source_id, content_snippet
            FROM raw_items
            WHERE id IN ({','.join('?' for _ in item_ids)})
              AND source = 'youtube'""",
        item_ids,
    ).fetchall()

    if not rows:
        return 0

    enriched = 0
    for row in rows[:BATCH_LIMIT]:
        video_id = row["source_id"]
        existing = row["content_snippet"] or ""

        if len(existing) > 600:
            continue

        transcript = _extract_captions(video_id)
        if not transcript or len(transcript) < 50:
            continue

        new_snippet = f"{existing}\n\n[Transcript] {transcript}"[:MAX_TRANSCRIPT_CHARS]
        conn.execute(
            "UPDATE raw_items SET content_snippet = ? WHERE id = ?",
            (new_snippet, row["id"]),
        )
        enriched += 1

    conn.commit()
    logger.info("Transcript: enriched %d/%d YouTube items", enriched, len(rows))
    return enriched
