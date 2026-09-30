"""养护资金决策与执行服务的 SQLite 表结构。"""

from __future__ import annotations

from .storage import Database

SCHEMA_FUNDING = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS road_segments (
    segment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    route_code TEXT NOT NULL,
    name TEXT NOT NULL,
    length_km REAL NOT NULL CHECK(length_km > 0),
    chainage_start REAL NOT NULL,
    chainage_end REAL NOT NULL CHECK(chainage_end > chainage_start),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS segment_evidence (
    evidence_id TEXT PRIMARY KEY,
    segment_id TEXT NOT NULL REFERENCES road_segments(segment_id),
    dimension TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_evidence_segment ON segment_evidence(segment_id, dimension, effective_at);
CREATE TABLE IF NOT EXISTS scoring_policies (
    policy_version TEXT PRIMARY KEY,
    rules_json TEXT NOT NULL,
    rules_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','active','retired')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funding_rounds (
    round_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    emergency_quota REAL NOT NULL DEFAULT 0 CHECK(emergency_quota >= 0),
    status TEXT NOT NULL CHECK(status IN ('open','frozen','finalized')),
    policy_version TEXT REFERENCES scoring_policies(policy_version),
    evidence_cutoff_at TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funding_envelopes (
    envelope_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    level TEXT NOT NULL CHECK(level IN ('central','provincial','county','emergency')),
    amount REAL NOT NULL CHECK(amount >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(round_id, level)
);
CREATE TABLE IF NOT EXISTS project_applications (
    application_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    segment_id TEXT NOT NULL REFERENCES road_segments(segment_id),
    title TEXT NOT NULL,
    amount_requested REAL NOT NULL CHECK(amount_requested > 0),
    chainage_start REAL NOT NULL,
    chainage_end REAL NOT NULL CHECK(chainage_end > chainage_start),
    work_type TEXT NOT NULL CHECK(work_type IN ('routine','emergency')),
    window_start TEXT,
    window_end TEXT,
    depends_on_json TEXT NOT NULL,
    milestones_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN (
        'submitted','ranked','approved','waitlisted','rejected',
        'cancelled','settled','emergency_pending','emergency_approved')),
    review_status TEXT NOT NULL DEFAULT 'not_required'
        CHECK(review_status IN ('not_required','pending','approved','rejected')),
    amount_approved REAL NOT NULL DEFAULT 0,
    allocation_json TEXT NOT NULL DEFAULT '{}',
    decision_reason TEXT,
    decided_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_apps_round ON project_applications(round_id);
CREATE INDEX IF NOT EXISTS idx_apps_segment ON project_applications(segment_id);
CREATE TABLE IF NOT EXISTS round_evidence_snapshots (
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    segment_id TEXT NOT NULL,
    dimension TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    effective_at TEXT NOT NULL,
    PRIMARY KEY(round_id, segment_id, dimension)
);
CREATE TABLE IF NOT EXISTS project_scores (
    round_id TEXT NOT NULL,
    application_id TEXT NOT NULL PRIMARY KEY,
    policy_version TEXT NOT NULL,
    eligible INTEGER NOT NULL,
    total_score REAL NOT NULL,
    rank INTEGER,
    dimension_scores_json TEXT NOT NULL,
    rationale_json TEXT NOT NULL,
    scored_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_milestones (
    milestone_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES project_applications(application_id),
    seq INTEGER NOT NULL CHECK(seq >= 1),
    name TEXT NOT NULL,
    amount REAL NOT NULL CHECK(amount > 0),
    due_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('planned','reserved','paid')),
    reserved_amount REAL NOT NULL DEFAULT 0,
    paid_amount REAL NOT NULL DEFAULT 0,
    UNIQUE(application_id, seq)
);
CREATE TABLE IF NOT EXISTS accounting_periods (
    period_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','closed')),
    opened_at TEXT NOT NULL,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS funding_ledger (
    entry_id TEXT PRIMARY KEY,
    period_id TEXT NOT NULL REFERENCES accounting_periods(period_id),
    envelope_id TEXT REFERENCES funding_envelopes(envelope_id),
    round_id TEXT NOT NULL,
    application_id TEXT NOT NULL,
    milestone_id TEXT,
    entry_type TEXT NOT NULL CHECK(entry_type IN (
        'commit','emergency_commit','reserve','payment',
        'change','cancel_release','recovery')),
    amount REAL NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_envelope ON funding_ledger(envelope_id);
CREATE INDEX IF NOT EXISTS idx_ledger_application ON funding_ledger(application_id);
CREATE TABLE IF NOT EXISTS independent_reviews (
    review_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL UNIQUE REFERENCES project_applications(application_id),
    reviewer_id TEXT NOT NULL,
    verdict TEXT NOT NULL CHECK(verdict IN ('approved','rejected')),
    notes TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS policy_simulations (
    simulation_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    policy_version TEXT NOT NULL,
    result_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def initialize_funding(database: Database) -> None:
    """在既有数据库上创建养护资金相关表（幂等）。"""

    database.connection.executescript(SCHEMA_FUNDING)
