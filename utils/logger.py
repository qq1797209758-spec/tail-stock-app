"""应用日志配置。"""

import logging
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent.parent / "output"
LOG_FILE = LOG_DIR / "app.log"


def get_logger(name: str) -> logging.Logger:
    """返回同时输出到控制台和文件的日志记录器。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)

    logger.addHandler(stream_handler)
    logger.addHandler(file_handler)
    logger.propagate = False
    return logger
