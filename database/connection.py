"""Database engine + session + idempotent init."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from config.settings import get_config
from .models import Base, SchemaVersion, utcnow

_ENGINE: Optional[Engine] = None
_DB_URL: Optional[str] = None


def _sqlalchemy_url(url: Optional[str] = None) -> str:
    if url:
        return url
    url = os.environ.get("DATABASE_URL") or get_config().database_url
    # Relative path interpretation: treat "sqlite:///./data/jobs.db" as project-root
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        # sqlite:///./data/jobs.db  -> sqlite:////absolute_path
        rel = url[len("sqlite:///"):]
        candidate = Path(rel)
        if not candidate.is_absolute():
            url = "sqlite:///" + str(candidate.resolve())
    return url


def get_engine(url: Optional[str] = None) -> Engine:
    """Return (and cache, keyed by URL) an Engine."""
    global _ENGINE, _DB_URL
    url = _sqlalchemy_url(url)
    if url == _DB_URL and _ENGINE is not None:
        return _ENGINE

    connect_args: dict = {}
    if url.startswith("sqlite"):
        # Use StaticPool so single-connection in-memory-style sessions reuse;
        # for file sqlite the default pooling works.
        connect_args["check_same_thread"] = False

    engine = create_engine(
        url,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )

    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_pragma_on_connect(dbapi_connection, connection_record):  # type: ignore
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

        @event.listens_for(engine, "commit")
        def _fk_pragma_after_commit_ensure(*_):
            pass

    _ENGINE, _DB_URL = engine, url
    return engine


def _ensure_str_dir(url: str) -> None:
    """For sqlite URLs, ensure the parent directory exists."""
    if not url.startswith("sqlite"):
        return
    suffix = "sqlite:///"
    rest = url[len(suffix):]
    path = Path(rest)
    path.parent.mkdir(parents=True, exist_ok=True)


def _bump_schema_version(session: Session) -> None:
    from .models import Base

    existing = session.query(SchemaVersion).order_by(SchemaVersion.id.desc()).first()
    if existing is None or existing.version != "2":
        session.add(SchemaVersion(version="2", applied_at=utcnow()))
    # In future this can migrate; for now we only add if missing.


def init_db(url: Optional[str] = None) -> Engine:
    """Idempotent initializer: creates tables if missing, bumps schema_version.

    Never drops existing data.
    """
    engine = get_engine(url)
    _ensure_str_dir(_sqlalchemy_url(url))
    # init_db() may be called after a previous command selected a different DB URL.
    # Import all models so they're registered on Base.metadata
    from . import models  # noqa: F401

    from .migrations import backup_before_upgrade
    backup_before_upgrade(engine)
    Base.metadata.create_all(bind=engine)

    # Apply backward-compatible column/table migrations
    from .repository import ensure_migrations
    ensure_migrations(engine)

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        _bump_schema_version(session)
        session.commit()
    return engine


def get_session(engine: Optional[Engine] = None) -> Session:
    """Create a Session for CLI / service use.

    ``expire_on_commit=False`` on purpose: this app fetches ORM rows,
    exits the `with with_session()` block (which commits), and then
    *reads attributes* of those rows while printing CLI output.
    With the SQLAlchemy default (`expire_on_commit=True`) the commit
    expires every instance and the post-block reads raise
    `DetachedInstanceError`. Disabling expiry keeps instances usable
    after commit; reads stay fresh because every CLI command
    re-queries the DB.
    """
    if engine is None:
        engine = get_engine()
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    return factory()


def session_scope(engine: Optional[Engine] = None) -> Iterator[Session]:
    """context-manager-friendly session helper."""
    if engine is None:
        engine = get_engine()
    # expire_on_commit=False for the same reason as get_session(): callers
    # may read ORM attributes of returned rows after the block exits.
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    at = factory()
    try:
        yield at
        at.commit()
    except Exception:
        at.rollback()
        raise
    finally:
        at.close()


def _reset() -> None:
    """For tests: drop cache of engine/session."""
    global _ENGINE, _DB_URL
    if _ENGINE is not None:
        try:
            _ENGINE.dispose()
        except Exception:
            pass
    _ENGINE = None
    _DB_URL = None


# module-level convenience: `engine` property driven lazily
class _EngineProxy:
    def __call__(self, url: Optional[str] = None) -> Engine:
        return get_engine(url)


engine = _EngineProxy()
