"""Persist / load user-editable SearchConfig via the SettingsRow table."""
from __future__ import annotations

import json
from typing import Any, Dict

from sqlalchemy.orm import Session

from config.search_config import SearchConfig

SEARCH_CONFIG_KEY = "search_config"
SEARCH_PROFILE_KEY_PREFIX = "search_profile:"


def _payload(cfg: SearchConfig) -> str:
    """Serialise every field for persistence.

    `normalized_dict()` is the hashing view and omits the title / location
    gate fields (primary_title, title_synonyms, require_verified_location,
    ...), so persisting it silently dropped those settings on every save.
    """
    return json.dumps(cfg.model_dump(mode="json", by_alias=True), sort_keys=True, separators=(",", ":"))


def save_search_config(session: Session, cfg: SearchConfig) -> None:
    """Persist SearchConfig (JSON) into the SettingsRow table."""
    from database.models import SettingsRow

    existing = session.query(SettingsRow).filter_by(key=SEARCH_CONFIG_KEY).first()
    payload = _payload(cfg)
    if existing is None:
        existing = SettingsRow(key=SEARCH_CONFIG_KEY, value=payload)
        session.add(existing)
    else:
        existing.value = payload
    session.commit()


def load_search_config(session: Session) -> Dict[str, Any] | None:
    from database.models import SettingsRow

    row = session.query(SettingsRow).filter_by(key=SEARCH_CONFIG_KEY).first()
    if row is None or not row.value:
        return None
    try:
        return json.loads(row.value)
    except json.JSONDecodeError:
        return None


def save_search_profile(session: Session, name: str, cfg: SearchConfig) -> None:
    """Persist a named search profile."""
    from database.models import SearchProfile

    existing = (
        session.query(SearchProfile).filter_by(name=name).first()
    )
    payload = _payload(cfg)
    if existing is None:
        search_profile = SearchProfile(name=name, config_json=payload)
        session.add(search_profile)
    else:
        existing.config_json = payload
    session.commit()


def load_search_profile(session: Session, name: str):
    """Return the named SearchProfile ORM row or None."""
    from database.models import SearchProfile

    return session.query(SearchProfile).filter_by(name=name).first()


def list_search_profiles(session: Session):
    from database.models import SearchProfile

    return session.query(SearchProfile).order_by(SearchProfile.name.asc()).all()