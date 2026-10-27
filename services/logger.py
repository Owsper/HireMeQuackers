"""Rotating-file logging helper (never logs secrets)."""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

_INITIALIZED = False


def setup_logging(level: str = "INFO", log_dir: str | os.PathLike | None = None) -> logging.Logger:
    """Configure root-ish logger for the app; rotating file log.

    Log file lives at `<log_dir>/app.log` if provided, else `data/logs/app.log`.
    """
    global _INITIALIZED
    if _INITIALIZED:
        return logging.getLogger("jobhunter")

    if log_dir is None:
        # Default: alongside project data dir
        from config.settings import PROJECT_ROOT

        log_dir = PROJECT_ROOT / "data" / "logs"

    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    log_file = log_path / "app.log"

    logger = logging.getLogger("jobhunter")
    if not logger.handlers:
        logger.setLevel(getattr(logging, level.upper(), logging.INFO))
        fmt = logging.Formatter(
            "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
        )
        fh = RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
        # Mirror to console at WARNING level to aid quick diagnosis but
        # never dump secrets: only strings we explicitly log are safe.
        sh = logging.StreamHandler()
        sh.setLevel(logging.WARNING)
        sh.setFormatter(fmt)
        logger.addHandler(sh)

    _INITIALIZED = True
    return logger


def get_logger(name: str = "jobhunter") -> logging.Logger:
    return logging.getLogger(name)
