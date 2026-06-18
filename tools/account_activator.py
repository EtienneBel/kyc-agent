"""
MCP Tool: account_activator
────────────────────────────
Creates and activates an account after KYC approval.
Logs the decision to the audit trail.
"""

import json
import logging
from difflib import SequenceMatcher
from uuid import UUID

from config import settings
from db import get_pool
from .a2a_escalator import escalate_to_human_review
from .scorer import compute_score

logger = logging.getLogger(__name__)


async def _compute_score_from_db(submission_id: str) -> dict:
    """
    Compute the KYC score deterministically from raw tool results stored in the DB.
    Called by activate_account so the LLM-supplied score is never trusted.
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT doc_confidence, face_confidence, face_match,
                   first_name, last_name, phone
            FROM kyc_submissions WHERE id = $1
            """,
            UUID(submission_id),
        )

        if not row:
            logger.warning(f"[account_activator] submission {submission_id} not found — using zero score")
            return compute_score(
                {"confidence": 0.0},
                {"match": False, "confidence": 0.0},
                {"is_duplicate": False},
                {"is_sanctioned": False},
            )

        doc_data = {"confidence": float(row["doc_confidence"] or 0.0)}
        face_result = {
            "match": bool(row["face_match"]),
            "confidence": float(row["face_confidence"] or 0.0),
        }

        # Re-query duplicate: same phone, different submission, not rejected, within 24 h
        dup_row = await conn.fetchrow(
            """
            SELECT id FROM kyc_submissions
            WHERE phone = $1
              AND id != $2
              AND decision != 'rejected'
              AND created_at > NOW() - INTERVAL '24 hours'
            LIMIT 1
            """,
            row["phone"],
            UUID(submission_id),
        )
        dup_result = {"is_duplicate": dup_row is not None}

        # Re-query sanctions by fuzzy name match
        is_sanctioned = False
        if row["first_name"] and row["last_name"]:
            full_name = f"{row['first_name']} {row['last_name']}".strip().lower()
            sanctions = await conn.fetch("SELECT full_name FROM sanctions_list")
            is_sanctioned = any(
                SequenceMatcher(None, full_name, s["full_name"].strip().lower()).ratio() >= 0.85
                for s in sanctions
            )

        return compute_score(doc_data, face_result, dup_result, {"is_sanctioned": is_sanctioned})


async def activate_account(
    submission_id: str,
    score: int,
    decision: str,
    reason: str,
    reviewed_by: str = "kyc-agent",
) -> dict:
    """
    Finalize a KYC submission — approve, reject, or escalate.

    Args:
        submission_id: UUID of the kyc_submission
        score:         Final risk score (0-100)
        decision:      approved | rejected | pending_review
        reason:        Human-readable decision explanation
        reviewed_by:   Agent name or human reviewer

    Returns:
        dict: { success, account_id, decision, message }
    """
    # Normalize LLM variants to the DB enum values
    _decision_map = {"escalate": "pending_review", "human_review": "pending_review", "review": "pending_review"}
    decision = _decision_map.get(decision, decision)
    if decision not in ("approved", "rejected", "pending_review"):
        decision = "pending_review"

    # For agent decisions, ignore the LLM-supplied score and compute it
    # deterministically from tool results stored in the DB.
    # Human reviewers (reviewed_by not in the agent set) keep their passed score.
    if reviewed_by in ("kyc-agent", "kyc-agent-fallback"):
        computed = await _compute_score_from_db(submission_id)
        score = computed["score"]
        decision = computed["decision"]
        logger.info(
            f"[account_activator] Server-computed score={score} decision={decision} "
            f"breakdown={computed['breakdown']} for {submission_id}"
        )
    else:
        score = max(0, min(100, score))

    logger.info(f"[account_activator] submission={submission_id} decision={decision} score={score}")

    pool = await get_pool()

    async with pool.acquire() as conn:
        async with conn.transaction():
            # ── 1. Update KYC submission ───────────────────────
            submission = await conn.fetchrow(
                """
                UPDATE kyc_submissions
                SET score = $1, decision = $2, decision_reason = $3, reviewed_by = $4
                WHERE id = $5
                RETURNING id, phone, first_name
                """,
                score,
                decision,
                reason,
                reviewed_by,
                UUID(submission_id),
            )

            if not submission:
                return {
                    "success": False,
                    "error": f"Submission {submission_id} not found",
                }

            account_id = None

            # ── 2. Create account only if approved ────────────
            if decision == "approved":
                existing = await conn.fetchrow(
                    "SELECT id FROM accounts WHERE phone = $1",
                    submission["phone"],
                )

                if not existing:
                    row = await conn.fetchrow(
                        """
                        INSERT INTO accounts (kyc_id, phone)
                        VALUES ($1, $2)
                        RETURNING id
                        """,
                        submission["id"],
                        submission["phone"],
                    )
                    account_id = str(row["id"])
                    logger.info(f"[account_activator] Account created: {account_id}")
                else:
                    account_id = str(existing["id"])

            # ── 3. Write to audit log ──────────────────────────
            await conn.execute(
                """
                INSERT INTO kyc_audit_log (submission_id, action, actor, details)
                VALUES ($1, $2, $3, $4::jsonb)
                """,
                submission["id"],
                f"kyc_{decision}",
                reviewed_by,
                json.dumps({
                    "score": score,
                    "reason": reason,
                    "account_id": account_id,
                }),
            )

    result = {
        "success": True,
        "account_id": account_id,
        "decision": decision,
        "phone": submission["phone"],
        "first_name": submission["first_name"],
        "message": _decision_message(decision, score),
    }

    # Auto-escalate to Human Review Agent — guaranteed regardless of whether
    # the LLM agent calls escalate_to_human_review explicitly.
    if decision == "pending_review":
        escalation = await escalate_to_human_review(
            submission_id=submission_id,
            phone=submission["phone"],
            score=score,
            reason=reason,
            document_data={},
            face_match_result={},
        )
        result["escalation_task_id"] = escalation.get("task_id")

    return result


def _decision_message(decision: str, score: int) -> str:
    messages = {
        "approved": f"Account activated successfully (score: {score}/100)",
        "rejected": f"Application rejected (score: {score}/100)",
        "pending_review": f"Escalated to human review (score: {score}/100)",
    }
    return messages.get(decision, f"Unknown decision: {decision}")
