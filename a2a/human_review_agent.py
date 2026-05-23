"""
A2A — Human Review Agent
─────────────────────────
A separate FastAPI service that receives escalated KYC cases
from the main KYC agent via the Agent-to-Agent (A2A) protocol.

When the KYC score is in the 70-94 range, the main agent delegates
here. A human reviewer accesses this interface to make the final call.

A2A Protocol:
  - Each agent exposes an AgentCard (/.well-known/agent.json)
  - Tasks are sent as POST /tasks
  - Results are fetched via GET /tasks/{task_id}
"""

import logging
import uuid
from datetime import datetime
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from db import get_pool
from tools.account_activator import activate_account
from tools.sms_sender import send_sms

logger = logging.getLogger(__name__)

app = FastAPI(title="KYC Human Review Agent", version="1.0.0")

# ── In-memory task store (use Redis in prod) ──────────────────
tasks: dict[str, dict] = {}


# ── A2A — Agent Card ──────────────────────────────────────────
AGENT_CARD = {
    "name": "human-review-agent",
    "description": (
        "Handles KYC cases that require human review. "
        "Accepts escalated submissions from the KYC agent and "
        "provides a review interface for compliance officers."
    ),
    "version": "1.0.0",
    "url": "http://localhost:8001",
    "capabilities": ["kyc_review", "manual_approval", "manual_rejection"],
    "input_schema": {
        "type": "object",
        "properties": {
            "submission_id": {"type": "string"},
            "phone": {"type": "string"},
            "score": {"type": "integer"},
            "reason": {"type": "string"},
            "document_data": {"type": "object"},
            "face_match_result": {"type": "object"},
        },
        "required": ["submission_id", "phone", "score"],
    },
}


# ── Schemas ───────────────────────────────────────────────────
class ReviewTask(BaseModel):
    submission_id: str
    phone: str
    score: int
    reason: str
    document_data: dict = {}
    face_match_result: dict = {}


class ReviewDecision(BaseModel):
    task_id: str
    decision: Literal["approved", "rejected"]
    reviewer: str
    notes: str = ""


# ── A2A Endpoints ─────────────────────────────────────────────

@app.get("/.well-known/agent.json")
async def agent_card():
    """A2A Agent Card — announces this agent's capabilities."""
    return AGENT_CARD


@app.post("/tasks", status_code=201)
async def create_review_task(task: ReviewTask):
    """
    Called by the KYC Agent (A2A) when a case needs human review.
    Creates a task and queues it for compliance officer review.
    """
    task_id = str(uuid.uuid4())

    tasks[task_id] = {
        "task_id": task_id,
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "payload": task.model_dump(),
        "decision": None,
    }

    logger.info(f"[human_review] Task created: {task_id} | submission={task.submission_id}")

    return {
        "task_id": task_id,
        "status": "pending",
        "message": "Task queued for human review",
    }


@app.get("/tasks/{task_id}")
async def get_task_status(task_id: str):
    """Poll task status — called by KYC Agent to check if review is complete."""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    return tasks[task_id]


@app.get("/tasks")
async def list_pending_tasks():
    """List all pending tasks — for the compliance officer dashboard."""
    return {
        "tasks": [
            t for t in tasks.values() if t["status"] == "pending"
        ],
        "total": len([t for t in tasks.values() if t["status"] == "pending"]),
    }


@app.post("/tasks/{task_id}/decide")
async def submit_decision(task_id: str, decision: ReviewDecision):
    """
    Compliance officer submits their review decision.
    Finalizes the KYC submission and notifies the applicant.
    """
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    payload = task["payload"]

    # Finalize in DB
    result = await activate_account(
        submission_id=payload["submission_id"],
        score=payload["score"],
        decision=decision.decision,
        reason=f"[Human Review by {decision.reviewer}] {decision.notes}",
        reviewed_by=decision.reviewer,
    )

    # Notify applicant
    template = "approved" if decision.decision == "approved" else "rejected"
    await send_sms(
        phone=payload["phone"],
        template=template,
        variables={
            "first_name": payload.get("document_data", {}).get("first_name", ""),
            "reason": decision.notes or "Vérification manuelle",
        },
    )

    # Update task
    tasks[task_id]["status"] = "completed"
    tasks[task_id]["decision"] = decision.model_dump()
    tasks[task_id]["completed_at"] = datetime.now().isoformat()

    logger.info(
        f"[human_review] Task {task_id} completed | "
        f"decision={decision.decision} | reviewer={decision.reviewer}"
    )

    return {
        "task_id": task_id,
        "status": "completed",
        "decision": decision.decision,
        "result": result,
    }


@app.get("/health")
async def health():
    return {"status": "ok", "agent": "human-review-agent"}
