"""封装 SQLite 连接、建表和事务边界。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actors (
    actor_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    active INTEGER NOT NULL CHECK(active IN (0, 1)),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sites (
    site_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    name TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version >= 1),
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS domain_records (
    record_id TEXT PRIMARY KEY,
    site_id TEXT NOT NULL REFERENCES sites(site_id),
    category TEXT NOT NULL,
    external_key TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES actors(actor_id),
    created_at TEXT NOT NULL,
    UNIQUE(site_id, category, external_key)
);
CREATE TABLE IF NOT EXISTS request_receipts (
    request_id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    action TEXT NOT NULL,
    resource_type TEXT NOT NULL,
    resource_id TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    occurred_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS road_segments (
    segment_id TEXT PRIMARY KEY,
    organization_id TEXT NOT NULL,
    route_code TEXT NOT NULL,
    start_km REAL NOT NULL,
    end_km REAL NOT NULL,
    name TEXT NOT NULL,
    sole_access INTEGER NOT NULL CHECK(sole_access IN (0, 1)),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CHECK(end_km > start_km)
);
CREATE TABLE IF NOT EXISTS scoring_policies (
    policy_id TEXT PRIMARY KEY,
    version_tag TEXT NOT NULL UNIQUE,
    spec_json TEXT NOT NULL,
    policy_hash TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS funding_rounds (
    round_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    bound_policy_id TEXT REFERENCES scoring_policies(policy_id),
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','frozen','approved','closed')),
    frozen_year INTEGER,
    evidence_hash TEXT,
    frozen_at TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS round_allocations (
    allocation_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    funding_level TEXT NOT NULL CHECK(funding_level IN ('central','provincial','county')),
    amount INTEGER NOT NULL CHECK(amount >= 0),
    emergency_reserve INTEGER NOT NULL DEFAULT 0 CHECK(emergency_reserve >= 0),
    created_at TEXT NOT NULL,
    UNIQUE(round_id, funding_level),
    CHECK(emergency_reserve <= amount)
);
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    external_key TEXT NOT NULL,
    title TEXT NOT NULL,
    organization_id TEXT NOT NULL,
    segment_id TEXT REFERENCES road_segments(segment_id),
    route_code TEXT NOT NULL,
    start_km REAL NOT NULL,
    end_km REAL NOT NULL,
    window_start TEXT,
    window_end TEXT,
    funding_level TEXT NOT NULL CHECK(funding_level IN ('central','provincial','county')),
    requested_amount INTEGER NOT NULL CHECK(requested_amount > 0),
    depends_on TEXT REFERENCES projects(project_id),
    emergency INTEGER NOT NULL DEFAULT 0 CHECK(emergency IN (0, 1)),
    evidence_json TEXT NOT NULL,
    evidence_hash TEXT NOT NULL,
    milestones_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('submitted','scored','obligated','in_progress',
                                         'deferred','rejected','cancelled','settled')),
    score REAL,
    rank INTEGER,
    policy_id TEXT REFERENCES scoring_policies(policy_id),
    contract_amount INTEGER,
    submitted_by TEXT NOT NULL,
    submitted_at TEXT NOT NULL,
    decided_at TEXT,
    UNIQUE(round_id, external_key)
);
CREATE TABLE IF NOT EXISTS project_scores (
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    policy_id TEXT NOT NULL REFERENCES scoring_policies(policy_id),
    factor_json TEXT NOT NULL,
    basis_json TEXT NOT NULL,
    total_score REAL NOT NULL,
    rank INTEGER NOT NULL,
    decision TEXT,
    reasons_json TEXT NOT NULL DEFAULT '[]',
    PRIMARY KEY(round_id, project_id)
);
CREATE TABLE IF NOT EXISTS project_flags (
    flag_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    project_a TEXT NOT NULL REFERENCES projects(project_id),
    project_b TEXT NOT NULL REFERENCES projects(project_id),
    flag_type TEXT NOT NULL CHECK(flag_type IN ('duplicate','split','window')),
    blocking INTEGER NOT NULL CHECK(blocking IN (0, 1)),
    phase TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS project_milestones (
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    code TEXT NOT NULL,
    position INTEGER NOT NULL,
    title TEXT NOT NULL,
    weight INTEGER NOT NULL CHECK(weight > 0 AND weight <= 100),
    retention_pct INTEGER NOT NULL CHECK(retention_pct BETWEEN 0 AND 100),
    planned_date TEXT,
    verified_by TEXT,
    verified_at TEXT,
    PRIMARY KEY(project_id, code)
);
CREATE TABLE IF NOT EXISTS project_reviews (
    review_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    reviewer_id TEXT NOT NULL,
    result TEXT NOT NULL CHECK(result IN ('approved','rejected')),
    note TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounting_periods (
    period_id TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    opened_by TEXT NOT NULL,
    opened_at TEXT NOT NULL,
    closed_by TEXT,
    closed_at TEXT
);
CREATE TABLE IF NOT EXISTS ledger_entries (
    entry_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL REFERENCES funding_rounds(round_id),
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    period_id TEXT NOT NULL REFERENCES accounting_periods(period_id),
    allocation_id TEXT NOT NULL REFERENCES round_allocations(allocation_id),
    funding_level TEXT NOT NULL,
    entry_type TEXT NOT NULL CHECK(entry_type IN ('commit','adjust','pay','release','recover')),
    amount INTEGER NOT NULL,
    detail_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
    payment_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL REFERENCES projects(project_id),
    milestone_code TEXT NOT NULL,
    period_id TEXT NOT NULL REFERENCES accounting_periods(period_id),
    gross INTEGER NOT NULL CHECK(gross >= 0),
    retained INTEGER NOT NULL CHECK(retained >= 0),
    released INTEGER NOT NULL CHECK(released >= 0),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(project_id, milestone_code)
);
"""


class Database:
    """管理 SQLite 数据库并为服务提供短事务。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        self.connection = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        """在异常时回滚，在成功时提交。"""

        self.connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def close(self) -> None:
        """关闭底层连接。"""

        self.connection.close()
