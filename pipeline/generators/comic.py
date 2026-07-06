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

    # Explicit per-panel script — a free-form "4컷만화 그려줘" prompt lets the
    # model improvise a 6-panel grid and duplicate content to fill it
    item1 = items[0] if len(items) > 0 else {}
    item2 = items[1] if len(items) > 1 else {}
    panel3 = (
        f"{item2.get('title', '')} — {item2.get('commentary', '')[:150]}"
        if item2 else summary[:200]
    )

    prompt = f"""하나의 이미지를 생성해줘: 2x2 그리드로 나뉜 정사각형 4컷만화. 칸은 정확히 4개이며, 각 칸의 내용은 아래에 위치별로 지정되어 있다.

스타일: 미소녀(분홍 머리)가 남학생에게 기술 투자 뉴스를 쉽게 설명해주는 학원물 만화. 비전공자 대학생 눈높이.

[왼쪽 위 칸 — 도입]
미소녀가 오늘의 헤드라인을 소개: "{headline}"

[오른쪽 위 칸 — 첫 번째 시그널]
{item1.get('title', '')} — {item1.get('commentary', '')[:150]}

[왼쪽 아래 칸 — 두 번째 시그널]
{panel3}

[오른쪽 아래 칸 — 결론]
핵심 인사이트 정리: "{takeaway}"

제약:
- 이미지 전체는 반드시 2x2 = 4칸. 다섯 번째 칸을 만들지 마라.
- 네 칸의 대사와 장면은 각각 위 지정 내용만 다루고, 칸끼리 중복 금지.
- 말풍선은 자연스러운 한국어, 오탈자 금지.
- 이미지 하단에 "{date_str} 기술 투자 시그널 다이제스트" 표기."""

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
