"""
MCP Tool: sms_sender
─────────────────────
Sends SMS notifications to applicants.

mock env           → logs to terminal (zero config)
prod env           → Africa's Talking API (West Africa native)
"""

import logging
from collections import defaultdict
from datetime import datetime

from config import settings

logger = logging.getLogger(__name__)

# ── Message templates ────────────────────────────────────────
TEMPLATES = {
    "approved": (
        "Bonjour {first_name}, votre identité a été vérifiée avec succès. "
        "Votre compte est maintenant actif. Bienvenue !"
    ),
    "rejected": (
        "Bonjour {first_name}, nous n'avons pas pu vérifier votre identité. "
        "Raison: {reason}. Veuillez soumettre un nouveau dossier ou nous contacter."
    ),
    "pending_review": (
        "Bonjour {first_name}, votre dossier est en cours de vérification manuelle. "
        "Vous serez notifié(e) dans les prochaines 24h."
    ),
    "otp": (
        "Votre code de confirmation KYC est: {otp}. "
        "Valable 10 minutes. Ne le partagez avec personne."
    ),
}


async def send_sms(
    phone: str,
    template: str,
    variables: dict | None = None,
) -> dict:
    """
    Send an SMS notification to the applicant.

    Args:
        phone:     Recipient phone (international format: +225XXXXXXXX)
        template:  Template key: approved | rejected | pending_review | otp
        variables: Dict of variables to inject into the template

    Returns:
        dict: { success, provider, message_id, preview }
    """
    variables = variables or {}
    message_body = TEMPLATES.get(template, template).format_map(defaultdict(str, variables))

    logger.info(f"[sms_sender] Sending '{template}' to {phone}")

    if settings.SMS_PROVIDER == "mock":
        return await _send_mock(phone, message_body)
    else:
        return await _send_africastalking(phone, message_body)


async def _send_mock(phone: str, message: str) -> dict:
    """Mock SMS — logs to terminal with visual formatting."""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print("\n" + "─" * 60)
    print(f"  [SMS MOCK] — {timestamp}")
    print(f"  To      : {phone}")
    print(f"  From    : {settings.AFRICASTALKING_SENDER_ID}")
    print(f"  Message : {message}")
    print("─" * 60 + "\n")

    return {
        "success": True,
        "provider": "mock",
        "message_id": f"mock_{datetime.now().timestamp()}",
        "preview": message,
    }


async def _send_africastalking(phone: str, message: str) -> dict:
    """Send via Africa's Talking API."""
    import httpx

    url = "https://api.africastalking.com/version1/messaging"
    headers = {
        "apiKey": settings.AFRICASTALKING_API_KEY,
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "username": settings.AFRICASTALKING_USERNAME,
        "to": phone,
        "message": message,
        "from": settings.AFRICASTALKING_SENDER_ID,
    }

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            response = await client.post(url, headers=headers, data=data)
            response.raise_for_status()
            result = response.json()

            recipients = result.get("SMSMessageData", {}).get("Recipients", [])
            if recipients:
                recipient = recipients[0]
                return {
                    "success": recipient.get("status") == "Success",
                    "provider": "africastalking",
                    "message_id": recipient.get("messageId"),
                    "preview": message,
                }

            return {"success": False, "provider": "africastalking", "error": str(result)}

        except Exception as e:
            logger.error(f"[sms_sender] Africa's Talking error: {e}")
            return {"success": False, "provider": "africastalking", "error": str(e)}
