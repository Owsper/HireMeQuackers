"""Record every AI operation into `AiUsage`."""
from __future__ import annotations

from typing import Optional
import logging

from database import repository as db


def record_usage(
    *,
    job_id: Optional[int] = None,
    operation: str = "score",
    model: Optional[str] = None,
    estimate_tokens: Optional[int] = None,
    cache_hit: bool = False,
    note: Optional[str] = None,
    session=None,
) -> Optional[object]:
    """Best-effort: silently skip if no session and cannot open one gracefully."""
    opened = session is None
    try:
        if session is None:
            from database.connection import get_engine, get_session

            engine = get_engine()
            session = get_session(engine)
        return db.record_ai_usage(
            session=session,
            job_id=job_id,
            operation=operation,
            model=model,
            estimate_tokens=estimate_tokens,
            cache_hit=cache_hit,
            note=note,
        )
    except Exception as exc:
        if session is not None:
            session.rollback()
        logging.getLogger("jobhunter.usage").warning("Usage recording failed: %s", type(exc).__name__)
        return None
    finally:
        if opened and session is not None:
            session.close()
