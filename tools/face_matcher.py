"""
MCP Tool: face_matcher
──────────────────────
Biometric comparison between the selfie and the document photo.
Uses DeepFace (open-source, on-premise) — no external API call.
Data never leaves the infrastructure.
"""

import logging
import tempfile
import base64
from pathlib import Path

from config import settings

logger = logging.getLogger(__name__)


class FaceMatchResult:
    def __init__(self, match: bool, confidence: float, distance: float, reason: str = ""):
        self.match = match
        self.confidence = confidence    # percentage 0-100
        self.distance = distance        # lower = more similar
        self.reason = reason

    def to_dict(self) -> dict:
        return {
            "match": self.match,
            "confidence": self.confidence,
            "distance": self.distance,
            "reason": self.reason,
        }


async def face_match(selfie_path: str, document_image_path: str) -> dict:
    """
    Compare face in selfie against face in document photo.

    Uses DeepFace with ArcFace model (best accuracy for African faces).

    Args:
        selfie_path:           Path to selfie image
        document_image_path:   Path to document image

    Returns:
        dict: { match, confidence, distance, reason }
    """
    logger.info(f"[face_matcher] Comparing selfie vs document")

    # Resolve actual file paths (handles base64 input too)
    selfie_file = _resolve_image_path(selfie_path, prefix="selfie")
    doc_file = _resolve_image_path(document_image_path, prefix="doc")

    try:
        # Import here to avoid slow startup if DeepFace not installed
        from deepface import DeepFace

        result = DeepFace.verify(
            img1_path=str(selfie_file),
            img2_path=str(doc_file),
            model_name="ArcFace",       # Best for African faces
            detector_backend="retinaface",
            enforce_detection=True,
            align=True,
        )

        verified = result.get("verified", False)
        distance = result.get("distance", 1.0)
        threshold = result.get("threshold", 0.68)

        # Convert distance to a confidence percentage
        # distance=0 → 100% confidence, distance≥threshold → 0%
        confidence = max(0.0, (1 - distance / threshold) * 100)
        confidence = round(min(confidence, 100.0), 2)

        below_min = confidence < settings.FACE_MATCH_MIN_CONFIDENCE

        return FaceMatchResult(
            match=verified and not below_min,
            confidence=confidence,
            distance=round(distance, 4),
            reason=(
                f"Confidence {confidence}% — "
                + ("MATCH" if verified else f"NO MATCH (threshold: {threshold})")
                + (f" — below minimum {settings.FACE_MATCH_MIN_CONFIDENCE}%" if below_min else "")
            ),
        ).to_dict()

    except Exception as e:
        logger.error(f"[face_matcher] DeepFace error: {e}")
        return FaceMatchResult(
            match=False,
            confidence=0.0,
            distance=1.0,
            reason=f"Face detection failed: {str(e)}",
        ).to_dict()

    finally:
        # Cleanup temp files if created
        for f in [selfie_file, doc_file]:
            if str(f).startswith(tempfile.gettempdir()):
                try:
                    f.unlink()
                except Exception:
                    pass


def _resolve_image_path(image_input: str, prefix: str = "img") -> Path:
    """
    If input is a file path → return as-is.
    If input is base64 → write to temp file and return path.
    """
    if Path(image_input).exists():
        return Path(image_input)

    # Assume base64
    data = image_input.split(",")[-1] if "," in image_input else image_input
    decoded = base64.b64decode(data)

    tmp = Path(tempfile.mktemp(prefix=f"kyc_{prefix}_", suffix=".jpg"))
    tmp.write_bytes(decoded)
    return tmp
