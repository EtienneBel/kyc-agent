"""
Agentic AI Frameworks Demo — Main Entry Point
──────────────────────────────────────────────
Demonstrates Google ADK, MCP tools, and A2A protocol on a KYC use case.

  Google ADK  — orchestrates the LlmAgent and tool-calling loop
  MCP tools   — typed async functions the agent invokes by name
  A2A         — delegates ambiguous cases to a separate Human Review Agent
"""

import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiofiles
from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from config import settings
from db import get_pool, close_pool
from agent import run_kyc_verification

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)


# ── Lifespan — DB pool management ────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"Starting KYC Agent | ENV={settings.APP_ENV} | MODEL={settings.active_model}")
    await get_pool()
    yield
    await close_pool()


# ── App ───────────────────────────────────────────────────────
app = FastAPI(
    title="Agentic AI Frameworks Demo — KYC",
    description=(
        "Demonstrates Google ADK, MCP tools, and A2A protocol. "
        "KYC verification is the example use case."
    ),
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Routes ────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "env": settings.APP_ENV,
        "model": settings.active_model,
    }


@app.post("/kyc/submit", status_code=202)
async def submit_kyc(
    phone: str = Form(..., description="Phone number in international format: +2250700000000"),
    document_image: UploadFile = File(..., description="CNI or passport image (JPEG/PNG)"),
    selfie: UploadFile = File(..., description="Live selfie of the applicant (JPEG/PNG)"),
):
    """
    Submit a new KYC verification request.

    The agent will:
    1. Extract data from the document
    2. Perform biometric face verification
    3. Check for duplicates
    4. Screen against sanctions lists
    5. Return a decision: approved | rejected | pending_review
    """
    # ── Save uploaded files ───────────────────────────────────
    submission_id = str(uuid.uuid4())
    doc_path = UPLOAD_DIR / f"{submission_id}_doc{Path(document_image.filename).suffix}"
    selfie_path = UPLOAD_DIR / f"{submission_id}_selfie{Path(selfie.filename).suffix}"

    async with aiofiles.open(doc_path, "wb") as f:
        await f.write(await document_image.read())

    async with aiofiles.open(selfie_path, "wb") as f:
        await f.write(await selfie.read())

    # ── Create submission record ──────────────────────────────
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kyc_submissions (id, phone, document_type, doc_image_path, selfie_path)
            VALUES ($1, $2, 'cni', $3, $4)
            """,
            uuid.UUID(submission_id),
            phone,
            str(doc_path),
            str(selfie_path),
        )

    logger.info(f"[main] KYC submission created: {submission_id} | phone={phone}")

    # ── Run the ADK agent ─────────────────────────────────────
    try:
        result = await run_kyc_verification(
            submission_id=submission_id,
            phone=phone,
            document_image_path=str(doc_path),
            selfie_path=str(selfie_path),
        )

        return {
            "submission_id": submission_id,
            "status": "processed",
            "model_used": result["model_used"],
            "summary": result["summary"],
        }

    except Exception as e:
        logger.error(f"[main] KYC agent error: {e}", exc_info=True)
        raise HTTPException(
            status_code=500,
            detail=f"KYC processing failed: {str(e)}",
        )


@app.get("/kyc/{submission_id}")
async def get_submission(submission_id: str):
    """Get the status and result of a KYC submission."""
    pool = await get_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, phone, first_name, last_name, score, decision,
                   decision_reason, created_at, updated_at
            FROM kyc_submissions
            WHERE id = $1
            """,
            uuid.UUID(submission_id),
        )

    if not row:
        raise HTTPException(status_code=404, detail="Submission not found")

    return dict(row)
