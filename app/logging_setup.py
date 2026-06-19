"""Structured local logging to autoapply.log (NFR-6).

Every send and classification is logged with timestamp, target, and outcome.
"""
from __future__ import annotations

import logging

from .config import LOG_PATH

_configured = False


def setup_logging() -> logging.Logger:
    global _configured
    logger = logging.getLogger("autoapply")
    if _configured:
        return logger

    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")

    fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    _configured = True
    return logger


def log_event(stage: str, target: str, outcome: str, detail: str = "") -> None:
    """Uniform structured line: stage | target | outcome | detail."""
    logger = setup_logging()
    msg = f"stage={stage} target={target} outcome={outcome}"
    if detail:
        msg += f" detail={detail}"
    logger.info(msg)
