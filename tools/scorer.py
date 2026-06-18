"""
Deterministic KYC score calculator.

Replaces LLM-invented scoring. Score is always computed from tool outputs,
never from model text.

Rubric (matches agent prompt):
  Document readable (confidence 0.0–1.0 → 0–20 pts)   max 20
  Face match (confidence 0–100, match=True required)    max 35
  No duplicate account                                  max 20
  No sanctions match                                    max 25
  TOTAL                                                max 100
"""

from config import settings


def compute_score(
    doc_data: dict,
    face_result: dict,
    dup_result: dict,
    sanctions_result: dict,
) -> dict:
    """
    Compute a deterministic KYC risk score from tool outputs.

    Args:
        doc_data:         Output of extract_document_data
        face_result:      Output of face_match
        dup_result:       Output of check_duplicate_account
        sanctions_result: Output of check_sanctions_list

    Returns:
        {
            "score": int (0-100),
            "breakdown": {"document": int, "face": int, "duplicate": int, "sanctions": int},
            "decision": "approved" | "pending_review" | "rejected"
        }
    """
    # ── Document readability (0–20 pts) ──────────────────────
    # confidence is 0.0–1.0 from document_extractor
    raw_confidence = doc_data.get("confidence", 0.0) if doc_data else 0.0
    doc_pts = round(raw_confidence * 20)

    # ── Face match (0–35 pts) ────────────────────────────────
    # confidence is 0–100 from face_matcher; only awarded if match=True
    face_match_ok = face_result.get("match", False) if face_result else False
    face_confidence = face_result.get("confidence", 0.0) if face_result else 0.0
    if face_match_ok:
        face_pts = round((face_confidence / 100) * 35)
    else:
        face_pts = 0

    # ── No duplicate (0 or 20 pts) ───────────────────────────
    is_dup = dup_result.get("is_duplicate", True) if dup_result else True
    dup_pts = 0 if is_dup else 20

    # ── No sanctions (0 or 25 pts) ───────────────────────────
    is_sanctioned = sanctions_result.get("is_sanctioned", True) if sanctions_result else True
    sanctions_pts = 0 if is_sanctioned else 25

    score = doc_pts + face_pts + dup_pts + sanctions_pts

    # Hard overrides: sanctions or duplicate → always reject regardless of score
    if is_sanctioned or is_dup:
        decision = "rejected"
    elif score >= settings.KYC_AUTO_APPROVE_THRESHOLD:
        decision = "approved"
    elif score >= settings.KYC_AUTO_REJECT_THRESHOLD:
        decision = "pending_review"
    else:
        decision = "rejected"

    return {
        "score": score,
        "breakdown": {
            "document": doc_pts,
            "face": face_pts,
            "duplicate": dup_pts,
            "sanctions": sanctions_pts,
        },
        "decision": decision,
    }
