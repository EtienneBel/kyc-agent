-- ─────────────────────────────────────────────────────────────
--  KYC Agent — Initial Schema
--  PostgreSQL 16
-- ─────────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ── Document types ────────────────────────────────────────────
CREATE TYPE document_type AS ENUM ('cni', 'passport', 'residence_permit');

-- ── KYC decision ──────────────────────────────────────────────
CREATE TYPE kyc_decision AS ENUM ('approved', 'rejected', 'pending_review');

-- ── KYC submissions ───────────────────────────────────────────
CREATE TABLE kyc_submissions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    phone           VARCHAR(20) NOT NULL,
    first_name      VARCHAR(100),
    last_name       VARCHAR(100),
    date_of_birth   DATE,
    nationality     VARCHAR(3),                         -- ISO 3166-1 alpha-3
    document_type   document_type NOT NULL,
    document_number VARCHAR(50),
    document_expiry DATE,
    doc_image_path  TEXT,                               -- path or object-storage key
    selfie_path     TEXT,
    score           INTEGER,                            -- 0-100
    decision        kyc_decision DEFAULT 'pending_review',
    decision_reason TEXT,
    reviewed_by     VARCHAR(100),                       -- human reviewer if escalated
    created_at      TIMESTAMPTZ DEFAULT NOW(),
    updated_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Accounts (activated after KYC approval) ───────────────────
CREATE TABLE accounts (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    kyc_id          UUID REFERENCES kyc_submissions(id) ON DELETE RESTRICT,
    phone           VARCHAR(20) UNIQUE NOT NULL,
    status          VARCHAR(20) DEFAULT 'active',
    activated_at    TIMESTAMPTZ DEFAULT NOW()
);

-- ── Sanctions watchlist (loaded from OFAC, local lists, etc.) ─
CREATE TABLE sanctions_list (
    id              SERIAL PRIMARY KEY,
    full_name       VARCHAR(200) NOT NULL,
    date_of_birth   DATE,
    nationality     VARCHAR(3),
    source          VARCHAR(50),                        -- 'OFAC', 'BCEAO', 'LOCAL'
    added_at        TIMESTAMPTZ DEFAULT NOW()
);

-- ── Audit log — every agent decision is traced ────────────────
CREATE TABLE kyc_audit_log (
    id              SERIAL PRIMARY KEY,
    submission_id   UUID REFERENCES kyc_submissions(id),
    action          VARCHAR(100) NOT NULL,
    actor           VARCHAR(100) DEFAULT 'kyc-agent',   -- agent name or human
    details         JSONB,
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

-- ── Indexes ───────────────────────────────────────────────────
CREATE INDEX idx_kyc_phone         ON kyc_submissions(phone);
CREATE INDEX idx_kyc_decision      ON kyc_submissions(decision);
CREATE INDEX idx_kyc_doc_number    ON kyc_submissions(document_number);
CREATE INDEX idx_sanctions_name    ON sanctions_list(full_name);
CREATE INDEX idx_audit_submission  ON kyc_audit_log(submission_id);

-- ── auto-update updated_at ────────────────────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_kyc_updated_at
    BEFORE UPDATE ON kyc_submissions
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── Seed: sample sanctions entries ───────────────────────────
INSERT INTO sanctions_list (full_name, nationality, source) VALUES
    ('John Doe Fraudster', 'NGA', 'LOCAL'),
    ('Jane Sanction Test', 'GHA', 'OFAC');
