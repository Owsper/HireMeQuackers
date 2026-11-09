"""Backward-compatible in-place database migrations. Idempotent."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import Engine, inspect, text

log = logging.getLogger(__name__)

EXTRACTION_COLUMNS = {
    "raw_description": "TEXT", "structured_requirements_json": "TEXT",
    "extraction_method": "VARCHAR(60)", "extraction_status": "VARCHAR(40)",
    "extracted_at": "DATETIME", "description_completeness": "FLOAT", "extraction_errors_json": "TEXT",
}


def backup_before_upgrade(engine: Engine) -> Path | None:
    """SQLite online backup includes WAL transactions. Never overwrite an existing backup."""
    if engine.dialect.name != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        return None
    path = Path(engine.url.database)
    tables = inspect(engine).get_table_names()
    if not path.exists() or "jobs" not in tables:
        return None
    columns = {c["name"] for c in inspect(engine).get_columns("jobs")}
    if set(EXTRACTION_COLUMNS) <= columns and "candidate_job_matches" in tables:
        return None
    dest = path.with_name(path.name + ".pre-v2-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f") + ".bak")
    with sqlite3.connect(path) as source, sqlite3.connect(dest) as target:
        source.backup(target)
    log.info("Database backup created before v2 migration: %s", dest.name)
    return dest


def _column_exists(engine: Engine, table: str, column: str) -> bool:
    insp = inspect(engine)
    if table not in insp.get_table_names():
        return False
    return column in [c["name"] for c in insp.get_columns(table)]


def upgrade(engine: Engine) -> None:
    """Apply all pending migrations. Safe to call repeatedly (idempotent)."""
    backup_before_upgrade(engine)
    # New v2 match table avoids destructively rebuilding the old SQLite unique constraint.
    from .models import JobMatch
    JobMatch.__table__.create(engine, checkfirst=True)
    with engine.begin() as conn:
        job_cols = {
            **EXTRACTION_COLUMNS,
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
            "description_language": "VARCHAR(10)",
            "required_languages_json": "TEXT",
        }
        if (_column_exists(engine, "scan_records", "id")
                and not _column_exists(engine, "scan_records", "platform_stats_json")):
            conn.execute(text("ALTER TABLE scan_records ADD COLUMN platform_stats_json TEXT"))
            log.info("Migrated: scan_records.platform_stats_json added")
        for col, coltype in job_cols.items():
            if "jobs" in inspect(engine).get_table_names() and not _column_exists(engine, "jobs", col):
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {col} {coltype}"))
                log.info("Migrated: jobs.%s added", col)

        if "job_matches" in inspect(engine).get_table_names():
            # Old scores have no trustworthy candidate identity. Keep them available as legacy history.
            cols = "id,job_id,match_score,score_json,matching_evidence_json,missing_requirements_json,ai_model,evaluated_at,profile_version,scoring_config_hash"
            selected = "legacy.id,legacy.job_id,legacy.match_score,legacy.score_json,legacy.matching_evidence_json,legacy.missing_requirements_json,legacy.ai_model,COALESCE(legacy.evaluated_at,CURRENT_TIMESTAMP),COALESCE(legacy.profile_version,1),legacy.scoring_config_hash"
            conn.execute(text(f"INSERT INTO candidate_job_matches ({cols}) SELECT {selected} FROM job_matches legacy WHERE NOT EXISTS (SELECT 1 FROM candidate_job_matches current WHERE current.id=legacy.id)"))

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
