"""
MCP Tool: duplicate_checker
────────────────────────────
Detects if a phone number or document number already exists
in the accounts or kyc_submissions tables.
Prevents one person from opening multiple accounts.
"""

import logging

from db import get_pool

logger = logging.getLogger(__name__)


async def check_duplicate_account(
    phone: str,
    document_number: str | None = None,
) -> dict:
    """
    Check if a phone number or document is already registered.

    Args:
        phone:           Phone number (international format: +2250700000000)
        document_number: Document ID number, optional

    Returns:
        dict: { is_duplicate, duplicate_type, existing_account_id }
    """
    logger.info(f"[duplicate_checker] Checking phone={phone}, doc={document_number}")

    pool = await get_pool()

    async with pool.acquire() as conn:
        # ── Check 1: Active account with same phone ────────────
        account = await conn.fetchrow(
            "SELECT id, status FROM accounts WHERE phone = $1",
            phone,
        )

        if account:
            logger.warning(f"[duplicate_checker] Duplicate phone: {phone}")
            return {
                "is_duplicate": True,
                "duplicate_type": "phone_already_registered",
                "existing_account_id": str(account["id"]),
                "status": account["status"],
            }

        # ── Check 2: Pending KYC with same phone ───────────────
        pending = await conn.fetchrow(
            """
            SELECT id, decision FROM kyc_submissions
            WHERE phone = $1 AND decision != 'rejected'
            ORDER BY created_at DESC LIMIT 1
            """,
            phone,
        )

        if pending:
            logger.warning(f"[duplicate_checker] Pending KYC for phone: {phone}")
            return {
                "is_duplicate": True,
                "duplicate_type": "pending_kyc_exists",
                "existing_account_id": str(pending["id"]),
                "status": pending["decision"],
            }

        # ── Check 3: Document number already used ─────────────
        if document_number:
            doc_match = await conn.fetchrow(
                """
                SELECT id FROM kyc_submissions
                WHERE document_number = $1 AND decision = 'approved'
                LIMIT 1
                """,
                document_number,
            )

            if doc_match:
                logger.warning(f"[duplicate_checker] Document already used: {document_number}")
                return {
                    "is_duplicate": True,
                    "duplicate_type": "document_already_used",
                    "existing_account_id": str(doc_match["id"]),
                    "status": "approved",
                }

    logger.info(f"[duplicate_checker] No duplicate found for {phone}")
    return {
        "is_duplicate": False,
        "duplicate_type": None,
        "existing_account_id": None,
        "status": None,
    }
