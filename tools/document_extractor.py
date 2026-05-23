"""
MCP Tool: document_extractor
─────────────────────────────
Extracts structured data from a CNI or passport image using Vision AI.

local env  → Gemma via Ollama (multimodal)
prod env   → Gemini 1.5 Flash Vision
"""

import base64
import json
import logging
from pathlib import Path

from google import genai
from google.genai import types
import httpx
from pydantic import BaseModel

from config import settings

logger = logging.getLogger(__name__)


class DocumentData(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    date_of_birth: str | None = None         # ISO format: YYYY-MM-DD
    document_number: str | None = None
    document_expiry: str | None = None       # ISO format: YYYY-MM-DD
    nationality: str | None = None           # ISO 3166-1 alpha-3
    document_type: str | None = None         # cni | passport | residence_permit
    confidence: float = 0.0                  # 0.0 - 1.0


EXTRACTION_PROMPT = """
You are a KYC document analysis system. Extract structured data from this identity document image.

Return ONLY a valid JSON object with these fields:
{
  "first_name": "string or null",
  "last_name": "string or null",
  "date_of_birth": "YYYY-MM-DD or null",
  "document_number": "string or null",
  "document_expiry": "YYYY-MM-DD or null",
  "nationality": "3-letter ISO code or null",
  "document_type": "cni | passport | residence_permit",
  "confidence": 0.0 to 1.0
}

Rules:
- confidence reflects how clearly the document is readable (glare, blur, angle)
- Return null for any field you cannot read clearly
- Do NOT invent data — only extract what is clearly visible
"""


async def extract_document_data(image_path: str) -> dict:
    """
    Extract identity data from a document image.

    Args:
        image_path: Local path or base64-encoded image string

    Returns:
        dict with extracted fields + confidence score
    """
    logger.info(f"[document_extractor] Processing: {image_path}")

    # Load image as base64
    image_b64 = _load_image_b64(image_path)

    if settings.APP_ENV == "prod":
        result = await _extract_with_gemini(image_b64)
    else:
        result = await _extract_with_ollama(image_b64)

    logger.info(f"[document_extractor] Result: {result}")
    return result


def _load_image_b64(image_path: str) -> str:
    """Load image from path and encode as base64."""
    if image_path.startswith("data:") or len(image_path) > 500:
        # Already base64
        return image_path.split(",")[-1] if "," in image_path else image_path

    path = Path(image_path)
    if not path.exists():
        raise FileNotFoundError(f"Image not found: {image_path}")

    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


async def _extract_with_gemini(image_b64: str) -> dict:
    """Use Gemini 1.5 Flash Vision (prod)."""
    client = genai.Client(api_key=settings.GEMINI_API_KEY)
    image_part = types.Part(
        inline_data=types.Blob(
            mime_type="image/jpeg",
            data=base64.b64decode(image_b64),
        )
    )
    response = await client.aio.models.generate_content(
        model="gemini-1.5-flash",
        contents=[EXTRACTION_PROMPT, image_part],
    )
    return _parse_json_response(response.text)


async def _extract_with_ollama(image_b64: str) -> dict:
    """Use Gemma multimodal via Ollama (local). Uses /api/chat — required for chat-template models like gemma3."""
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{settings.OLLAMA_BASE_URL}/api/chat",
            json={
                "model": settings.OLLAMA_MODEL,
                "messages": [
                    {
                        "role": "user",
                        "content": EXTRACTION_PROMPT,
                        "images": [image_b64],
                    }
                ],
                "stream": False,
            },
        )
        response.raise_for_status()
        data = response.json()
        return _parse_json_response(data.get("message", {}).get("content", "{}"))


def _parse_json_response(raw: str) -> dict:
    """Parse JSON from model response, stripping markdown fences if any."""
    clean = raw.strip()
    if clean.startswith("```"):
        lines = clean.split("\n")
        clean = "\n".join(lines[1:-1])
    try:
        parsed = json.loads(clean)
        doc = DocumentData(**parsed)
        return doc.model_dump()
    except Exception as e:
        logger.error(f"[document_extractor] Failed to parse response: {e}\nRaw: {raw}")
        return DocumentData().model_dump()
