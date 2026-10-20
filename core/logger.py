"""
Logger setup – thread-safe, emits Qt signals for live UI console.
"""

import logging
import queue
from datetime import datetime
from pathlib import Path

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATE_FORMAT = "%H:%M:%S"

# Global log queue for UI consumption
log_queue: queue.Queue = queue.Queue()


class QueueHandler(logging.Handler):
    """Sends log records to a queue so the UI thread can display them."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record: logging.LogRecord):
        msg = self.format(record)
        level = record.levelname
        self.q.put((level, msg))


def get_logger(name: str, log_dir: Path | None = None) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    # Console
    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Queue (for UI)
    qh = QueueHandler(log_queue)
    qh.setLevel(logging.DEBUG)
    qh.setFormatter(fmt)
    logger.addHandler(qh)

    # File (optional)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(
            log_dir / f"session_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log",
            encoding="utf-8",
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger
