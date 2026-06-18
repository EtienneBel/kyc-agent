-- ─────────────────────────────────────────────────────────────
--  KYC Agent — Migration 003
--  Store raw tool results so score can be computed server-side
-- ─────────────────────────────────────────────────────────────

ALTER TABLE kyc_submissions
    ADD COLUMN IF NOT EXISTS doc_confidence   FLOAT,
    ADD COLUMN IF NOT EXISTS face_confidence  FLOAT,
    ADD COLUMN IF NOT EXISTS face_match       BOOLEAN;
