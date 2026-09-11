"""Logging configuration shared by every entry point.

Every operation, warning and failure goes to two places: the console
(stdout) and a log file, so a run can always be reconstructed after the
fact even if the terminal output was lost. The file handler always keeps
DEBUG-level detail regardless of ``--verbose``, so ``--verbose`` only
controls how chatty the console is.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

#: Root logger name for the whole project; every module logs under it
#: (``orario.data``, ``orario.model``, ``orario.main``, ...) so a single
#: ``configure_logging`` call controls all of them.
ROOT_LOGGER_NAME = "orario"

_LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(log_path: Path, verbose: bool = False) -> logging.Logger:
    """Attach a console handler and a file handler to the root logger.

    ``log_path``'s parent directory is created if missing. Safe to call more
    than once (e.g. from tests): existing handlers on the project logger are
    replaced rather than duplicated.
    """
    logger = logging.getLogger(ROOT_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    logger.debug("Logging configurato: file=%s verbose=%s", log_path, verbose)
    return logger
