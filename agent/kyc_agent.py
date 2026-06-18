"""
KYC Agent — Built with Google ADK
──────────────────────────────────
Orchestrates the full KYC verification pipeline using:
  - Google ADK for agent orchestration
  - MCP tools (extract, face match, sanctions, duplicate check, activate, SMS)
  - A2A delegation for human review (ambiguous cases)
  - Gemma (local) or Gemini (prod) as the reasoning model
"""

import json
import logging
import re
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
from tools.scorer import compute_score
from tools import (
    extract_document_data,
    face_match,
    check_sanctions_list,
    check_duplicate_account,
    activate_account,
    send_sms,
    escalate_to_human_review,  # registered as agent tool; also used indirectly via activate_account
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

    # ── Guaranteed finalisation ───────────────────────────────
    # gemma3:4b sometimes drops mid-sequence tool calls (writes a JSON summary
    # in text but never calls activate_account). Parse the text output to
    # recover the intended decision/score before falling back.
    parsed = _parse_decision_from_text(final_response)
    await _ensure_finalised_if_pending(submission_id, phone, parsed)

    return {
        "submission_id": submission_id,
        "summary": final_response,
        "model_used": settings.active_model,
        "env": settings.APP_ENV,
    }


def _parse_decision_from_text(text: str) -> dict | None:
    """Extract decision/score from Gemma's JSON text output when tool call was dropped."""
    try:
        match = re.search(r'\{[^{}]*"decision"[^{}]*\}', text, re.DOTALL)
        if match:
            data = json.loads(match.group())
            args = data.get("args", data)
            decision = args.get("decision")
            score = args.get("score")
            reason = args.get("reason", "")
            if decision in ("approved", "rejected", "pending_review") and isinstance(score, int):
                return {"decision": decision, "score": score, "reason": reason}
    except Exception:
        pass
    return None


async def _ensure_finalised_if_pending(submission_id: str, phone: str, parsed: dict | None) -> None:
    """Write final decision to DB if Gemma dropped the tool call mid-sequence."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT decision, score FROM kyc_submissions WHERE id = $1",
            UUID(submission_id),
        )

    if not row or row["decision"] != "pending_review" or row["score"] is not None:
        return

    # Try parsed text decision first, then fall back to computed score from DB data
    if parsed and parsed.get("decision") in ("approved", "rejected", "pending_review"):
        decision = parsed["decision"]
        score = parsed["score"]
        reason = parsed.get("reason", "Agent output parsed — tool call dropped")
        logger.info(
            f"[kyc_agent] Applying parsed decision={decision} score={score} for {submission_id}"
        )
    else:
        # Load tool results from DB and compute score deterministically
        pool = await get_pool()
        async with pool.acquire() as conn:
            sub = await conn.fetchrow(
                """
                SELECT first_name, last_name, document_number, nationality
                FROM kyc_submissions WHERE id = $1
                """,
                UUID(submission_id),
            )

        # Build minimal doc_data from what's in DB (written by extract_document_data in Task 2)
        doc_data = {}
        if sub and sub["first_name"]:
            doc_data = {"confidence": 0.6}  # conservative: readable but uncertain

        computed = compute_score(
            doc_data=doc_data,
            face_result={"match": False, "confidence": 0.0},  # unknown — conservative
            dup_result={"is_duplicate": False},
            sanctions_result={"is_sanctioned": False},
        )
        decision = computed["decision"]
        score = computed["score"]
        reason = f"Escalade automatique — agent interrompu. Score calculé: {score}/100"
        logger.info(
            f"[kyc_agent] Computed fallback score={score} decision={decision} for {submission_id}"
        )

    await activate_account(
        submission_id=submission_id,
        score=score,
        decision=decision,
        reason=reason,
        reviewed_by="kyc-agent-fallback",
    )
