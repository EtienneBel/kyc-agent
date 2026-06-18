# KYC Audit Fixes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all critical, high, and medium issues found in the June 2026 functional audit so the project works correctly end-to-end before the demo.

**Architecture:** Each tool writes its result to the DB immediately (extract_document_data → DB, activate_account reads from DB). Score is computed deterministically in Python, not by the LLM. The LLM still orchestrates tool calls, but Python owns data persistence and arithmetic.

**Tech Stack:** Python 3.11, FastAPI, asyncpg, PostgreSQL 16, Google ADK, DeepFace, LiteLLM (Gemma/Gemini)

## Global Constraints

- No breaking changes to the FastAPI request/response schema seen by external callers.
- No changes to `.env` keys — add new optional ones only.
- All DB writes go through asyncpg with the shared pool (`db.get_pool()`).
- No test framework exists — verify each task by running a `curl` submission and inspecting logs + DB.
- Docker volumes mount `.` → `/app`, so file edits apply without rebuild (uvicorn `--reload` is active).
- Never commit; the user will do that manually.
- Write reasoning in French in agent output (already the case), but code comments in English.

---

## File Map

| File | What changes |
|------|-------------|
| `tools/scorer.py` | **NEW** — deterministic score calculation |
| `tools/document_extractor.py` | Write extracted fields to DB; normalize confidence to 0–100 |
| `tools/account_activator.py` | Validate score range + score/decision alignment |
| `tools/a2a_escalator.py` | Load document_data from DB before POSTing to human review |
| `tools/duplicate_checker.py` | Add 24h time window to pending-KYC check |
| `tools/sanctions_checker.py` | Remove misleading OFAC stub; document scope clearly |
| `tools/sms_sender.py` | Remove emoji; ensure templates are safe with empty variables |
| `tools/face_matcher.py` | Distinguish "no face detected" from library crash |
| `agent/kyc_agent.py` | Use `compute_score()` in fallback; update `_ensure_finalised_if_pending` |
| `agent/prompts.py` | Align scoring rubric language with actual implementation |
| `db/migrations/002_add_constraints.sql` | **NEW** — CHECK constraint on score column |

---

## Task 1: Deterministic Score Calculator

**Files:**
- Create: `tools/scorer.py`

**Interfaces:**
- Produces: `compute_score(doc_data: dict, face_result: dict, dup_result: dict, sanctions_result: dict) -> dict`
- Return type: `{"score": int, "breakdown": {"document": int, "face": int, "duplicate": int, "sanctions": int}, "decision": str}`
- `decision` is one of `"approved" | "pending_review" | "rejected"` based on `settings.KYC_AUTO_APPROVE_THRESHOLD` (95) and `settings.KYC_AUTO_REJECT_THRESHOLD` (70)

**Why this task first:** Every other fix that needs a reliable score depends on this function.

- [ ] **Step 1: Create `tools/scorer.py`**

```python
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
```

- [ ] **Step 2: Add `compute_score` to `tools/__init__.py` exports**

Read `tools/__init__.py`, then add the import so other modules can use `from tools import compute_score`.

Open `tools/__init__.py` and add:
```python
from .scorer import compute_score
```

- [ ] **Step 3: Verify manually**

Run in the container:
```bash
docker exec kyc_agent python3 -c "
from tools.scorer import compute_score
r = compute_score(
    doc_data={'confidence': 0.95},
    face_result={'match': True, 'confidence': 85.0},
    dup_result={'is_duplicate': False},
    sanctions_result={'is_sanctioned': False},
)
print(r)
# Expected: score=19+29+20+25=93, decision='pending_review'
"
```

Expected output: `{'score': 93, 'breakdown': {'document': 19, 'face': 29, 'duplicate': 20, 'sanctions': 25}, 'decision': 'pending_review'}`

---

## Task 2: extract_document_data Writes Fields to DB

**Files:**
- Modify: `tools/document_extractor.py`

**Interfaces:**
- Consumes: nothing new — derives `submission_id` from the image filename (format: `uploads/UUID_doc.ext`) created by `main.py`
- Produces: same return dict as before, but now also writes to `kyc_submissions` table

**Why:** Document fields (first_name, last_name, document_number, etc.) are currently never persisted. Fixing this here means all downstream code (A2A, human review, SMS) automatically gets real data.

- [ ] **Step 1: Add DB write helper to `tools/document_extractor.py`**

Add `import uuid` and `from db import get_pool` imports at the top. Then add this helper function before `_parse_json_response`:

```python
def _parse_submission_id(image_path: str) -> str | None:
    """
    Derive submission_id from image filename.
    main.py creates files as: uploads/{UUID}_doc.{ext}
    Returns the UUID string, or None if pattern doesn't match.
    """
    try:
        stem = Path(image_path).stem  # e.g. "abc123-..._doc"
        base = stem.rsplit("_", 1)[0]  # e.g. "abc123-..."
        uuid.UUID(base)               # validates UUID format
        return base
    except (ValueError, AttributeError, IndexError):
        return None


async def _persist_to_db(submission_id: str, doc: dict) -> None:
    """Write extracted document fields to kyc_submissions."""
    pool = await get_pool()

    # Parse dates — extractor returns ISO strings (YYYY-MM-DD) or None
    from datetime import date

    def parse_date(val: str | None):
        if not val:
            return None
        try:
            return date.fromisoformat(val[:10])
        except ValueError:
            return None

    doc_type = doc.get("document_type")
    valid_types = {"cni", "passport", "residence_permit"}
    if doc_type not in valid_types:
        doc_type = None  # leave existing value

    async with pool.acquire() as conn:
        await conn.execute(
            """
            UPDATE kyc_submissions SET
                first_name      = COALESCE($2, first_name),
                last_name       = COALESCE($3, last_name),
                date_of_birth   = COALESCE($4, date_of_birth),
                document_number = COALESCE($5, document_number),
                document_expiry = COALESCE($6, document_expiry),
                nationality     = COALESCE($7, nationality),
                document_type   = COALESCE($8::document_type, document_type)
            WHERE id = $1
            """,
            uuid.UUID(submission_id),
            doc.get("first_name"),
            doc.get("last_name"),
            parse_date(doc.get("date_of_birth")),
            doc.get("document_number"),
            parse_date(doc.get("document_expiry")),
            doc.get("nationality"),
            doc_type,
        )
```

- [ ] **Step 2: Call `_persist_to_db` at the end of `extract_document_data`**

Replace the return line in `extract_document_data` (currently just `return result`) with:

```python
    logger.info(f"[document_extractor] Result: {result}")

    # Persist extracted fields immediately — don't wait for activate_account
    submission_id = _parse_submission_id(image_path)
    if submission_id:
        try:
            await _persist_to_db(submission_id, result)
            logger.info(f"[document_extractor] Persisted to DB: submission={submission_id}")
        except Exception as e:
            logger.error(f"[document_extractor] DB write failed: {e}")
            # Don't fail the tool — return result even if DB write fails

    return result
```

- [ ] **Step 3: Normalize confidence to 0–100 in `DocumentData` model**

The `DocumentData.confidence` field is documented as `0.0–1.0`. `compute_score()` also expects `0.0–1.0`. Keep it as-is — the scorer handles the conversion (`raw_confidence * 20`). No change needed here.

- [ ] **Step 4: Verify DB write**

Submit a KYC request, then check DB:
```bash
curl -s -X POST http://localhost:8000/kyc/submit \
  -F "phone=+22600000050" \
  -F "document_image=@/app/files/passeport.png" \
  -F "selfie=@/app/files/photo.jpg"

docker exec kyc_postgres psql -U kyc_user -d kyc_agent -c \
  "SELECT phone, first_name, last_name, document_number, document_type FROM kyc_submissions ORDER BY created_at DESC LIMIT 1;"
```

Expected: `first_name`, `last_name`, `document_number` are no longer NULL.

---

## Task 3: activate_account — Validate Score and Decision

**Files:**
- Modify: `tools/account_activator.py`

**Interfaces:**
- Consumes: same signature as before (`submission_id, score, decision, reason, reviewed_by`)
- Adds: score clamping, decision/score consistency enforcement using settings thresholds

**Why:** Without validation, the LLM can write `decision="approved"` with `score=10`. The DB has no constraint yet (Task 10 adds it). We enforce in code first.

- [ ] **Step 1: Add score clamping and decision correction in `activate_account`**

After the `_decision_map` normalization block (after line ~42), add:

```python
    # Clamp score to valid range
    score = max(0, min(100, score))

    # Auto-correct decision if it contradicts the score
    # Only apply when reviewed_by is the agent (not a human reviewer override)
    if reviewed_by in ("kyc-agent", "kyc-agent-fallback"):
        if score >= settings.KYC_AUTO_APPROVE_THRESHOLD and decision != "approved":
            logger.warning(
                f"[account_activator] Score {score} qualifies for approval but decision={decision}. Auto-correcting."
            )
            decision = "approved"
        elif score < settings.KYC_AUTO_REJECT_THRESHOLD and decision == "approved":
            logger.warning(
                f"[account_activator] Score {score} below rejection threshold but decision=approved. Auto-correcting to rejected."
            )
            decision = "rejected"
```

- [ ] **Step 2: Verify**

After submission, check that DB decision matches the score range. No extra curl needed — covered by Task 2 verification.

---

## Task 4: a2a_escalator Loads Document Data from DB

**Files:**
- Modify: `tools/a2a_escalator.py`

**Interfaces:**
- Consumes: same signature — `submission_id, phone, score, reason, document_data, face_match_result`
- Change: when `document_data` is empty, load from DB using `submission_id`

**Why:** Currently `activate_account` always passes `document_data={}` to the escalator, so human reviewers see no document fields and SMS greetings are empty.

- [ ] **Step 1: Add DB load helper to `a2a_escalator.py`**

Add imports `import uuid` and `from db import get_pool` at the top. Then add this helper:

```python
async def _load_document_data(submission_id: str) -> dict:
    """Load extracted document fields from DB when caller didn't provide them."""
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT first_name, last_name, date_of_birth, document_number,
                       document_expiry, nationality, document_type
                FROM kyc_submissions WHERE id = $1
                """,
                uuid.UUID(submission_id),
            )
        if row:
            return {k: str(v) if v is not None else None for k, v in dict(row).items()}
    except Exception as e:
        logger.error(f"[a2a_escalator] Failed to load document data: {e}")
    return {}
```

- [ ] **Step 2: Call `_load_document_data` when `document_data` is empty**

In `escalate_to_human_review`, before building `payload`, add:

```python
    if not document_data:
        document_data = await _load_document_data(submission_id)
```

- [ ] **Step 3: Verify human review task has document data**

Submit a KYC, then check the human review task:
```bash
# Get the task list from human review agent
curl -s http://localhost:8001/tasks | python3 -m json.tool | grep -A 20 '"payload"'
```

Expected: `document_data` contains `first_name`, `last_name`, `document_number` — not empty dict.

---

## Task 5: Fallback Uses compute_score()

**Files:**
- Modify: `agent/kyc_agent.py`

**Interfaces:**
- Consumes: `compute_score` from `tools.scorer`
- Consumes: DB rows for tool results (loaded in fallback when Gemma dropped)

**Why:** When Gemma drops mid-sequence, the fallback currently uses score=0 or parses a potentially hallucinated score from Gemma's text. After Tasks 1 and 2, the DB has real extracted document data. We can compute a real score from that.

- [ ] **Step 1: Add import at top of `agent/kyc_agent.py`**

```python
from tools.scorer import compute_score
```

- [ ] **Step 2: Replace `_ensure_finalised_if_pending` with version that uses `compute_score`**

Replace the entire function with:

```python
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
```

- [ ] **Step 3: Verify no import errors on reload**

```bash
docker compose logs kyc-agent --tail=5
```

Expected: No `ImportError` or `AttributeError` — server restarts cleanly.

---

## Task 6: Duplicate Checker — 24-Hour Time Window

**Files:**
- Modify: `tools/duplicate_checker.py`

**Interfaces:**
- No signature change — behavior change only

**Why:** A `pending_review` submission from weeks ago blocks new attempts forever. Only block within a rolling 24h window.

- [ ] **Step 1: Update the pending KYC query in `check_duplicate_account`**

Replace the pending check query (currently lines 51–57):

```python
        # ── Check 2: Pending KYC with same phone (within 24h) ─
        pending = await conn.fetchrow(
            """
            SELECT id, decision FROM kyc_submissions
            WHERE phone = $1
              AND decision != 'rejected'
              AND created_at > NOW() - INTERVAL '24 hours'
            ORDER BY created_at DESC LIMIT 1
            """,
            phone,
        )
```

- [ ] **Step 2: Verify**

The existing `pending_review` rows from previous test sessions are all older than 24h (or at least from earlier today). Submit with a previously-used phone:

```bash
curl -s -X POST http://localhost:8000/kyc/submit \
  -F "phone=+22600000005" \
  -F "document_image=@/app/files/passeport.png" \
  -F "selfie=@/app/files/photo.jpg"
```

Expected: no "Un compte similaire" rejection from a stale old row. (If the test was run within the last 24h, it will still block — that is correct behavior.)

---

## Task 7: Sanctions Checker — Remove Misleading OFAC Stub

**Files:**
- Modify: `tools/sanctions_checker.py`

**Why:** The commented-out OFAC block and the `_check_ofac` stub that returns `[]` gives a false impression of OFAC coverage. Remove it entirely. Document scope honestly.

- [ ] **Step 1: Remove the commented OFAC block and stub function**

In `check_sanctions_list`, remove lines:
```python
    # ── 2. OFAC SDN (open public API) ─────────────────────────
    # Uncomment in prod — adds ~200ms latency
    # ofac_matches = await _check_ofac(full_name, date_of_birth)
    # sources_checked.append("OFAC")
    # matches.extend(ofac_matches)
```

Delete the entire `_check_ofac` function (lines 128–148).

- [ ] **Step 2: Update the module docstring**

Replace the module docstring at the top:
```python
"""
MCP Tool: sanctions_checker
────────────────────────────
Cross-references a person against the local PostgreSQL sanctions table.

Scope: LOCAL_DB only (seeded from db/migrations/001_init.sql).
OFAC and BCEAO integrations are not implemented — add them here when needed.
"""
```

- [ ] **Step 3: Verify module loads cleanly**

```bash
docker exec kyc_agent python3 -c "from tools.sanctions_checker import check_sanctions_list; print('OK')"
```

Expected: `OK`

---

## Task 8: SMS — Remove Emoji, Ensure Safe Templates

**Files:**
- Modify: `tools/sms_sender.py`

**Why:** `🎉` may not render on feature phones (common in West Africa). Templates already use `defaultdict(str, ...)` so missing variables are safe — just verify.

- [ ] **Step 1: Remove emoji from approved template**

Change the `"approved"` template from:
```python
    "approved": (
        "Bonjour {first_name}, votre identité a été vérifiée avec succès. "
        "Votre compte est maintenant actif. Bienvenue ! 🎉"
    ),
```
To:
```python
    "approved": (
        "Bonjour {first_name}, votre identité a été vérifiée avec succès. "
        "Votre compte est maintenant actif. Bienvenue !"
    ),
```

- [ ] **Step 2: Remove emoji from mock display**

In `_send_mock`, change `📱 SMS MOCK` to `[SMS MOCK]`:
```python
    print(f"  [SMS MOCK] — {timestamp}")
```

(The `📱` emoji in logs is also unreliable across terminals.)

- [ ] **Step 3: Verify template renders with empty first_name**

```bash
docker exec kyc_agent python3 -c "
from collections import defaultdict
from tools.sms_sender import TEMPLATES
msg = TEMPLATES['approved'].format_map(defaultdict(str, {}))
print(repr(msg))
assert '🎉' not in msg
assert '{first_name}' not in msg
print('OK')
"
```

Expected: prints the message with empty first_name and `OK` — no braces, no emoji.

---

## Task 9: face_matcher — Distinguish Error Types

**Files:**
- Modify: `tools/face_matcher.py`

**Why:** Any DeepFace failure currently returns `match=False, confidence=0.0`. The agent can't tell whether the image was bad, no face was detected, or the library crashed. Add reason clarity.

- [ ] **Step 1: Split the exception handler in `face_match`**

Replace the single `except Exception as e:` block (lines 89–96) with:

```python
    except ValueError as e:
        # No face detected in one or both images — not a library error
        err_msg = str(e)
        logger.warning(f"[face_matcher] No face detected: {err_msg}")
        return FaceMatchResult(
            match=False,
            confidence=0.0,
            distance=1.0,
            reason=f"Aucun visage détecté dans l'image: {err_msg}",
        ).to_dict()

    except Exception as e:
        err_msg = str(e)
        logger.error(f"[face_matcher] DeepFace error: {err_msg}")
        return FaceMatchResult(
            match=False,
            confidence=0.0,
            distance=1.0,
            reason=f"Erreur technique lors de la vérification biométrique: {err_msg}",
        ).to_dict()
```

- [ ] **Step 2: Add DeepFace timeout guard**

DeepFace has no timeout — it can hang. Wrap the `DeepFace.verify` call:

```python
        import asyncio
        loop = asyncio.get_event_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: DeepFace.verify(
                    img1_path=str(selfie_file),
                    img2_path=str(doc_file),
                    model_name="ArcFace",
                    detector_backend="retinaface",
                    enforce_detection=True,
                    align=True,
                ),
            ),
            timeout=30.0,
        )
```

Replace the synchronous `DeepFace.verify(...)` call with this async-wrapped version.

- [ ] **Step 3: Verify no import errors**

```bash
docker compose logs kyc-agent --tail=3
```

Expected: clean reload, no errors.

---

## Task 10: DB Migration — Score CHECK Constraint

**Files:**
- Create: `db/migrations/002_add_constraints.sql`

**Why:** The code validates score range in `activate_account` (Task 3), but the DB has no CHECK constraint. Adding it provides a last-line-of-defense guarantee.

- [ ] **Step 1: Create migration file**

```sql
-- ─────────────────────────────────────────────────────────────
--  KYC Agent — Migration 002
--  Add score range constraint
-- ─────────────────────────────────────────────────────────────

ALTER TABLE kyc_submissions
    ADD CONSTRAINT chk_score_range
    CHECK (score IS NULL OR (score >= 0 AND score <= 100));
```

- [ ] **Step 2: Apply migration to running container**

```bash
docker exec -i kyc_postgres psql -U kyc_user -d kyc_agent \
  < /Users/macbookpro/Projects/my-projects/kyc-agent/db/migrations/002_add_constraints.sql
```

- [ ] **Step 3: Verify constraint exists**

```bash
docker exec kyc_postgres psql -U kyc_user -d kyc_agent -c \
  "\d kyc_submissions" | grep chk_score
```

Expected: `chk_score_range` appears in the output.

---

## End-to-End Verification

After all 10 tasks, run a full KYC submission and verify the entire chain:

```bash
# Submit
RESPONSE=$(curl -s -X POST http://localhost:8000/kyc/submit \
  -F "phone=+22600000077" \
  -F "document_image=@/Users/macbookpro/Projects/my-projects/kyc-agent/files/passeport.png" \
  -F "selfie=@/Users/macbookpro/Projects/my-projects/kyc-agent/files/photo.jpg")
echo $RESPONSE
SID=$(echo $RESPONSE | python3 -c "import sys,json; print(json.load(sys.stdin)['submission_id'])")

# Check DB has all fields populated
docker exec kyc_postgres psql -U kyc_user -d kyc_agent -c \
  "SELECT phone, first_name, last_name, document_number, document_type, score, decision FROM kyc_submissions WHERE id='$SID';"

# Check human review task has document_data (if escalated)
curl -s http://localhost:8001/tasks | python3 -m json.tool
```

**All passing criteria:**
- [ ] `first_name`, `last_name`, `document_number` NOT NULL in DB
- [ ] `score` is in range 0–100
- [ ] Human review task payload has `document_data` with real fields
- [ ] SMS log shows applicant name (not empty "Bonjour ,")
- [ ] No emoji in SMS output
- [ ] No `libGL` or `tf-keras` errors in logs
- [ ] Retry with same phone after 25h works (or immediately if previous was >24h ago)
