"""
KYC Agent — Built with Google ADK
──────────────────────────────────
Orchestrates the full KYC verification pipeline using:
  - Google ADK for agent orchestration
  - MCP tools (extract, face match, sanctions, duplicate check, activate, SMS)
  - A2A delegation for human review (ambiguous cases)
  - Gemma (local) or Gemini (prod) as the reasoning model
"""

import logging
from typing import Any
from uuid import UUID

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.runners import InMemoryRunner
from google.genai import types
import litellm

from config import settings
from db import get_pool
from agent.prompts import KYC_AGENT_INSTRUCTION
from tools import (
    extract_document_data,
    face_match,
    check_sanctions_list,
    check_duplicate_account,
    activate_account,
    send_sms,
    escalate_to_human_review,
)

logger = logging.getLogger(__name__)

# ── LiteLLM config — routes to Gemma (local) or Gemini (prod) ─
if settings.APP_ENV == "local":
    litellm.api_base = settings.OLLAMA_BASE_URL
else:
    litellm.api_key = settings.GEMINI_API_KEY


def build_kyc_agent() -> LlmAgent:
    """
    Builds and returns the KYC LlmAgent with all MCP tools registered.

    The agent uses:
      - LiteLLM as the model backend (Gemma or Gemini, env-driven)
      - FunctionTool wrappers around each MCP tool function
      - InMemorySessionService for per-submission state
    """
    agent = LlmAgent(
        name="kyc_agent",
        model=settings.active_model,
        description=(
            "KYC verification agent for fintech. "
            "Verifies identity documents, performs biometric checks, "
            "screens against sanctions lists, and activates accounts."
        ),
        instruction=KYC_AGENT_INSTRUCTION,
        tools=[
            FunctionTool(func=extract_document_data),
            FunctionTool(func=face_match),
            FunctionTool(func=check_sanctions_list),
            FunctionTool(func=check_duplicate_account),
            FunctionTool(func=activate_account),
            FunctionTool(func=escalate_to_human_review),
            FunctionTool(func=send_sms),
        ],
    )
    return agent


async def run_kyc_verification(
    submission_id: str,
    phone: str,
    document_image_path: str,
    selfie_path: str,
) -> dict[str, Any]:
    """
    Run the full KYC verification pipeline for a submission.

    Args:
        submission_id:        UUID of the kyc_submission record
        phone:                Applicant phone number
        document_image_path:  Path to the uploaded document image
        selfie_path:          Path to the uploaded selfie

    Returns:
        dict with final decision, score, and agent summary
    """
    logger.info(
        f"[kyc_agent] Starting KYC | submission={submission_id} | phone={phone} | env={settings.APP_ENV}"
    )

    agent = build_kyc_agent()
    runner = InMemoryRunner(agent=agent)

    session_id = f"kyc_{submission_id.replace('-', '_')}"
    await runner.session_service.create_session(
        app_name=runner.app_name,
        user_id="system",
        session_id=session_id,
    )

    user_message = types.Content(
        role="user",
        parts=[types.Part(text=f"""
Veuillez vérifier le dossier KYC suivant :

- Submission ID   : {submission_id}
- Téléphone       : {phone}
- Document image  : {document_image_path}
- Selfie          : {selfie_path}

Effectuez toutes les vérifications requises dans l'ordre et prenez une décision finale.
""")],
    )

    final_response = ""

    # Run the agent — it will call tools autonomously
    async for event in runner.run_async(
        user_id="system",
        session_id=session_id,
        new_message=user_message,
    ):
        if hasattr(event, "content") and event.content:
            for part in event.content.parts:
                if hasattr(part, "text") and part.text:
                    final_response += part.text

    logger.info(f"[kyc_agent] Completed | submission={submission_id}")
    logger.info(f"[kyc_agent] Summary:\n{final_response}")

    # ── Guaranteed A2A escalation ─────────────────────────────
    # gemma3:4b sometimes drops mid-sequence tool calls. If the submission
    # still has decision=pending_review and no score, escalate now.
    await _ensure_escalated_if_pending(submission_id, phone)

    return {
        "submission_id": submission_id,
        "summary": final_response,
        "model_used": settings.active_model,
        "env": settings.APP_ENV,
    }


async def _ensure_escalated_if_pending(submission_id: str, phone: str) -> None:
    """Escalate to human review if the agent left the submission in pending_review without a score."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decision, score FROM kyc_submissions WHERE id = $1",
            UUID(submission_id),
        )

    if not row or row["decision"] != "pending_review" or row["score"] is not None:
        return

    logger.info(f"[kyc_agent] Agent dropped mid-sequence — auto-escalating {submission_id}")
    await escalate_to_human_review(
        submission_id=submission_id,
        phone=phone,
        score=0,
        reason="Escalade automatique — agent interrompu en cours de séquence",
        document_data={},
        face_match_result={},
    )
