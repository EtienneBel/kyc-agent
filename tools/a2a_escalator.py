"""
MCP Tool: a2a_escalator
────────────────────────
Escalates a KYC case to the Human Review Agent via A2A protocol.
Called by the KYC agent when score is 70-94 (pending_review decision).
"""

import logging

import httpx

from config import settings

logger = logging.getLogger(__name__)


async def escalate_to_human_review(
    submission_id: str,
    phone: str,
    score: int,
    reason: str,
    document_data: dict,
    face_match_result: dict,
) -> dict:
    """
    Send a KYC case to the Human Review Agent for manual compliance review.
    Call this whenever decision is pending_review (score 70-94).

    Args:
        submission_id:    UUID of the kyc_submission record
        phone:            Applicant phone number
        score:            Final computed risk score (0-100)
        reason:           Why this case needs human review
        document_data:    Output from extract_document_data
        face_match_result: Output from face_match

    Returns:
        dict: { task_id, status, message } from the human review agent
    """
    payload = {
        "submission_id": submission_id,
        "phone": phone,
        "score": score,
        "reason": reason,
        "document_data": document_data,
        "face_match_result": face_match_result,
    }

    logger.info(
        f"[a2a_escalator] Escalating submission={submission_id} score={score} "
        f"to {settings.HUMAN_REVIEW_AGENT_URL}"
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(
                f"{settings.HUMAN_REVIEW_AGENT_URL}/tasks",
                json=payload,
            )
            response.raise_for_status()
            result = response.json()
            logger.info(
                f"[a2a_escalator] Task created: {result.get('task_id')} | "
                f"submission={submission_id}"
            )
            return result
        except Exception as e:
            logger.error(f"[a2a_escalator] Failed to escalate: {e}")
            return {
                "task_id": None,
                "status": "error",
                "message": f"Escalation failed: {e}",
            }
