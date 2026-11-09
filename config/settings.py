"""Application-wide configuration (env / .env).

Pydantic-settings is not required; we parse environment variables manually
for zero extra dependencies.
"""
from __future__ import annotations

import os
import logging
from pathlib import Path
from typing import Any, Optional

try:  # python-dotenv is optional at import time (tests may not need it)
    from dotenv import load_dotenv
except Exception:  # pragma: no cover - dotenv should be installed in real env
    load_dotenv = None  # type: ignore

from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent


_ENV_LOADED = False


def _load_env_file() -> None:
    """Load .env from the project root (cwd as fallback), once per process.

    override=False: variables already present in the OS environment win.
    Edits made while the app runs take effect on the next start.
    """
    global _ENV_LOADED
    if load_dotenv is None or _ENV_LOADED:
        return
    _ENV_LOADED = True
    for base in (PROJECT_ROOT, Path.cwd()):
        env_file = base / ".env"
        if env_file.exists():
            try:
                load_dotenv(env_file, override=False)
            except (OSError, ValueError) as exc:
                logging.getLogger(__name__).warning("Could not load .env: %s", type(exc).__name__)
            break


class AppConfig(BaseModel):
    """Root application configuration.

    All fields have sane defaults; missing values are populated from
    environment (and .env) using env-var names in UPPERCASE.

    The discovery-engine fields (search_provider, brave_search_api_key, etc.)
    are process-level *defaults*.  Per-search values live on
    ``SearchConfig`` in :mod:`config.search_config` (user-authored).
    """

    app_env: str = "local"
    log_level: str = "INFO"

    # auto | gemini | openai | anthropic | openrouter | groq | ollama
    ai_provider: str = "auto"
    ai_model: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    openai_api_key: str = ""
    openai_base_url: str = ""
    anthropic_api_key: str = ""
    openrouter_api_key: str = ""
    groq_api_key: str = ""
    ollama_base_url: str = "http://localhost:11434/v1"
    enable_ai_matching: bool = True
    enable_ai_direction: bool = False
    ai_timeout_seconds: float = Field(default=45.0, gt=0, le=300)
    ai_max_attempts: int = Field(default=3, ge=1, le=5)
    ai_backoff_seconds: float = Field(default=1.0, ge=0, le=30)
    extraction_refresh_hours: float = Field(default=168, ge=0)
    extraction_retry_hours: float = Field(default=6, ge=0)
    matching_weights: dict[str, float] = Field(default_factory=lambda: {
        "required_skills": 35, "experience": 25, "qualifications": 10,
        "preferred_skills": 10, "location": 10, "languages": 10,
    })

    @field_validator("matching_weights")
    @classmethod
    def _weights(cls, value):
        import math
        allowed = {"required_skills", "experience", "qualifications", "preferred_skills", "location", "languages"}
        if set(value) != allowed or any(not math.isfinite(v) or v < 0 for v in value.values()) or not sum(value.values()):
            raise ValueError("MATCHING_WEIGHTS must contain the six scoring categories with finite nonnegative weights and a positive sum")
        return value

    @field_validator("gemini_api_key", "openai_api_key", "anthropic_api_key", "openrouter_api_key", "groq_api_key", "ai_model", "ai_provider")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()

    database_url: str = "sqlite:///./data/jobs.db"
    data_dir: Path = Path("data")

    request_timeout_seconds: int = 15
    min_retry_attempts: int = 3
    rate_limit_min_seconds: float = 1.0
    max_response_bytes: int = 3 * 1024 * 1024

    scan_max_sources: int = 20
    scan_request_cap_per_source: int = 30

    # -- max-coverage discovery engine (process-level defaults) --
    search_provider: str = "brave"  # brave | exa | duckduckgo
    brave_search_api_key: str = ""
    exa_api_key: str = ""
    scan_budget_seconds: float = 300.0
    scan_max_total_queries: int = 40
    enrichment_max_per_scan: int = 25
    enrichment_delay_seconds: float = 1.5

    enable_pdf_export: bool = True
    enable_playwright: bool = False

    # Optional EURES public API endpoint.  None => EURES source skipped.
    eures_api_url: Optional[str] = None

    @field_validator("data_dir")
    @classmethod
    def _coerce_data_dir(cls, v: Any) -> Path:
        return Path(v) if not isinstance(v, Path) else v


def get_config() -> AppConfig:
    """Build AppConfig from environment (.env) + explicit overrides."""
    _load_env_file()

    def _env(name: str, default: str = "") -> str:
        return os.getenv(name, default)

    def _env_bool(name: str, default: bool) -> bool:
        raw = _env(name, str(default))
        return raw.strip().lower() in {"1", "true", "t", "yes", "y", "on"}

    def _env_int(name: str, default: int) -> int:
        raw = _env(name, str(default))
        try:
            return int(raw)
        except ValueError:
            return default

    def _env_float(name: str, default: float) -> float:
        raw = _env(name, str(default))
        try:
            return float(raw)
        except ValueError:
            return default

    import json
    try:
        weights = json.loads(_env("MATCHING_WEIGHTS", "null"))
    except json.JSONDecodeError:
        raise ValueError("MATCHING_WEIGHTS must be valid JSON") from None
    extra = {"matching_weights": weights} if weights is not None else {}
    return AppConfig(
        **extra,
        app_env=_env("APP_ENV", "local"),
        log_level=_env("LOG_LEVEL", "INFO"),
        ai_provider=_env("AI_PROVIDER", "auto"),
        ai_model=_env("AI_MODEL", ""),
        gemini_api_key=_env("GEMINI_API_KEY", ""),
        gemini_model=_env("GEMINI_MODEL", "gemini-3.8-flash"),
        openai_api_key=_env("OPENAI_API_KEY", ""),
        openai_base_url=_env("OPENAI_BASE_URL", ""),
        anthropic_api_key=_env("ANTHROPIC_API_KEY", ""),
        openrouter_api_key=_env("OPENROUTER_API_KEY", ""),
        groq_api_key=_env("GROQ_API_KEY", ""),
        ollama_base_url=_env("OLLAMA_BASE_URL", "http://localhost:11434/v1"),
        enable_ai_matching=_env_bool("ENABLE_AI_MATCHING", True),
        enable_ai_direction=_env_bool("ENABLE_AI_DIRECTION", False),
        ai_timeout_seconds=_env_float("AI_TIMEOUT_SECONDS", 45),
        ai_max_attempts=_env_int("AI_MAX_ATTEMPTS", 3),
        ai_backoff_seconds=_env_float("AI_BACKOFF_SECONDS", 1),
        extraction_refresh_hours=_env_float("EXTRACTION_REFRESH_HOURS", 168),
        extraction_retry_hours=_env_float("EXTRACTION_RETRY_HOURS", 6),
        database_url=_env("DATABASE_URL", "sqlite:///./data/jobs.db"),
        data_dir=Path(_env("DATA_DIR", str(PROJECT_ROOT / "data"))),
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 15),
        min_retry_attempts=_env_int("MAX_RETRY_ATTEMPTS", 3),
        rate_limit_min_seconds=_env_float("RATE_LIMIT_MIN_SECONDS", 1.0),
        max_response_bytes=_env_int("MAX_RESPONSE_BYTES", 3 * 1024 * 1024),
        scan_max_sources=_env_int("SCAN_MAX_SOURCES", 20),
        scan_request_cap_per_source=_env_int("SCAN_REQUEST_CAP_PER_SOURCE", 30),
        search_provider=_env("SEARCH_PROVIDER", "brave"),
        brave_search_api_key=_env("BRAVE_SEARCH_API_KEY", ""),
        exa_api_key=_env("EXA_API_KEY", ""),
        scan_budget_seconds=_env_float("SCAN_BUDGET_SECONDS", 300.0),
        scan_max_total_queries=_env_int("SCAN_MAX_TOTAL_QUERIES", 40),
        enrichment_max_per_scan=_env_int("ENRICHMENT_MAX_PER_SCAN", 25),
        enrichment_delay_seconds=_env_float("ENRICHMENT_DELAY_SECONDS", 1.5),
        enable_pdf_export=_env_bool("ENABLE_PDF_EXPORT", True),
        enable_playwright=_env_bool("ENABLE_PLAYWRIGHT", False),
        eures_api_url=_env("EURES_API_URL", "") or None,
    )
