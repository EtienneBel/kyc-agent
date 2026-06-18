-- ─────────────────────────────────────────────────────────────
--  KYC Agent — Migration 002
--  Add score range constraint
-- ─────────────────────────────────────────────────────────────

ALTER TABLE kyc_submissions
    ADD CONSTRAINT chk_score_range
    CHECK (score IS NULL OR (score >= 0 AND score <= 100));
