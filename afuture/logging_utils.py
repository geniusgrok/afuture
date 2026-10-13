"""运行日志配置。"""

import logging
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(path: str | Path, *, logger_name: str = "afuture") -> logging.Logger:
    """创建带滚动文件处理器的应用日志记录器。"""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(
            log_path,
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


@contextmanager
def replay_logging(path: str | Path):
    """Scope application logs for a sequential replay and restore its caller's setup."""
    logger = logging.getLogger("afuture")
    handlers, level, propagate = logger.handlers[:], logger.level, logger.propagate
    logger.handlers = []
    logger.propagate = False
    try:
        yield configure_logging(path)
    finally:
        replay_handlers = logger.handlers
        logger.handlers, logger.level, logger.propagate = handlers, level, propagate
        for handler in replay_handlers:
            handler.close()
