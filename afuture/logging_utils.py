"""运行日志配置。"""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_logging(path: str | Path) -> logging.Logger:
    """创建带滚动文件处理器的应用日志记录器。"""
    log_path = Path(path)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("afuture")
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
