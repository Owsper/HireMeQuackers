"""Backward-compatible in-place database migrations. Idempotent."""
from __future__ import annotations

import logging

from sqlalchemy import Engine, inspect, text

log = logging.getLogger(__name__)


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade(engine: Engine) -> None:
    """Apply all pending migrations. Safe to call repeatedly (idempotent)."""
    with engine.begin() as conn:
        job_cols = {
            "external_job_id": "VARCHAR(120)",
            "source_type": "VARCHAR(40)",
            "verification_status": "VARCHAR(30) DEFAULT 'unverified'",
            "extraction_confidence": "FLOAT",
            "last_verified_at": "DATETIME",
            "application_method": "VARCHAR(60)",
            "scam_risk_flags": "TEXT",
            "title_relevance": "VARCHAR(20)",
            "location_status": "VARCHAR(20)",
            "match_explanation": "TEXT",
        }
        for col, coltype in job_cols.items():
            if not _column_exists(engine, "jobs", col):
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}"))
                log.info("Migrated: jobs.%s added", col)

        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS scan_source_health (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_name VARCHAR(60) UNIQUE NOT NULL,
                last_scanned_at DATETIME,
                last_error TEXT,
                listings_found INTEGER DEFAULT 0,
                listings_stale INTEGER DEFAULT 0,
                is_healthy BOOLEAN DEFAULT 1
            )
            """
        ))
        conn.execute(text(
            """
            CREATE TABLE IF NOT EXISTS source_registry (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                domain VARCHAR(200) UNIQUE NOT NULL,
                ats_type VARCHAR(40),
                board_slug VARCHAR(120),
                discovered_at DATETIME NOT NULL,
                discovered_via VARCHAR(40) DEFAULT 'manual',
                is_active BOOLEAN DEFAULT 1
            )
            """
        ))
    log.info("Database migrations complete")
