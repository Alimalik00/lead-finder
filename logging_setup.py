"""Logging configuration.

Two sinks:

* **Console** -- concise, and routed through :mod:`tqdm` so log lines never
  smear a live progress bar.
* **Rotating file** -- verbose, with module/line info for debugging a run
  after the fact.

Third-party loggers (``urllib3``, ``requests``, ``anthropic``) are quietened to
WARNING so their per-request chatter does not drown out our own output.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path
from typing import Optional

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover - tqdm is a hard requirement in practice
    tqdm = None  # type: ignore[assignment]

CONSOLE_FORMAT = "%(asctime)s  %(levelname)-7s  %(message)s"
FILE_FORMAT = "%(asctime)s  %(levelname)-7s  %(name)s:%(lineno)d  %(message)s"
DATE_FORMAT = "%H:%M:%S"

_NOISY_LOGGERS = (
    "urllib3",
    "requests",
    "chardet",
    "charset_normalizer",
    "anthropic",
    "httpx",
    "httpcore",
)


class TqdmLoggingHandler(logging.Handler):
    """Emit log records via ``tqdm.write`` so progress bars stay intact."""

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D102
        try:
            message = self.format(record)
            if tqdm is not None:
                tqdm.write(message)
            else:  # pragma: no cover - fallback when tqdm is missing
                print(message)
        except Exception:  # pragma: no cover - never let logging crash the app
            self.handleError(record)


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
    quiet: bool = False,
) -> logging.Logger:
    """Configure the root logger and return the application logger.

    Args:
        level: Console log level name (``DEBUG``/``INFO``/``WARNING``/...).
        log_file: Optional path for the rotating debug log. Parent directories
            are created automatically.
        quiet: Raise the console threshold to ``WARNING`` regardless of *level*.

    Returns:
        The ``lead_finder`` logger.
    """
    numeric_level = getattr(logging, str(level).upper(), logging.INFO)
    console_level = logging.WARNING if quiet else numeric_level

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)  # Handlers apply their own thresholds.
    for handler in list(root.handlers):
        root.removeHandler(handler)

    console = TqdmLoggingHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter(CONSOLE_FORMAT, datefmt=DATE_FORMAT))
    root.addHandler(console)

    if log_file is not None:
        try:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            )
            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(logging.Formatter(FILE_FORMAT))
            root.addHandler(file_handler)
        except OSError as exc:  # pragma: no cover - e.g. read-only filesystem
            console.handle(
                logging.LogRecord(
                    name="lead_finder",
                    level=logging.WARNING,
                    pathname=__file__,
                    lineno=0,
                    msg="Could not open log file %s (%s); continuing without it.",
                    args=(log_file, exc),
                    exc_info=None,
                )
            )

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)

    return logging.getLogger("lead_finder")


def get_logger(name: str) -> logging.Logger:
    """Return a child logger under the ``lead_finder`` namespace."""
    return logging.getLogger(f"lead_finder.{name}")
