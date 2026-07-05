"""4-panel manga comic for daily digest via Gemini image generation."""
import logging
import os
import tempfile

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> genai.Client:
    global _client
    if _client is None:
        api_key = os.environ.get("GEMINI_API_KEY", "")
        if not api_key:
            return None
        _client = genai.Client(api_key=api_key)
    return _client


def generate_digest_comic(digest_data: dict, date_str: str) -> str | None:
    client = _get_client()
    if not client or not digest_data:
        return None

    headline = digest_data.get("headline", "")
    summary = digest_data.get("summary", "")
    takeaway = digest_data.get("one_line_takeaway", "")
    items = digest_data.get("top_items_commentary", [])
    items_text = "\n".join(
        f"- {it.get('title', '')}: {it.get('commentary', '')}"
        for it in items[:5]
    )

    content = f"""[{date_str} 기술 투자 시그널 다이제스트]

헤드라인: {headline}

요약: {summary}

주요 항목:
{items_text}

핵심 인사이트: {takeaway}"""

    prompt = f"""{content}

해당 내용을 비전공자인 대학생이 이해할 수 있는 난이도로 미소녀가 설명해주는 4컷만화를 생성해줘. 말풍선 텍스트는 반드시 한국어로 작성해줘."""

    try:
        resp = client.models.generate_content(
            model="gemini-3-pro-image",
            contents=prompt,
            config=types.GenerateContentConfig(
                response_modalities=["TEXT", "IMAGE"],
            ),
        )
        for part in resp.candidates[0].content.parts:
            if part.inline_data and part.inline_data.mime_type.startswith("image/"):
                ext = part.inline_data.mime_type.split("/")[-1]
                path = tempfile.mktemp(suffix=f"_digest_comic_{date_str}.{ext}")
                with open(path, "wb") as f:
                    f.write(part.inline_data.data)
                logger.info("Digest comic generated: %s", path)
                return path

        logger.warning("Gemini response contained no image parts")
        return None
    except Exception:
        logger.exception("Digest comic generation failed")
        return None
