import logging
import sys
from pathlib import Path

import config


def get_logger(name: str) -> logging.Logger:
    """Return a configured logger."""

    logger = logging.getLogger(name)

    if logger.handlers:
        return logger

    logger.propagate = False

    log_level = getattr(logging, config.LOG_LEVEL.upper(), logging.INFO)
    logger.setLevel(log_level)

    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    Path(config.LOG_FILE).parent.mkdir(parents=True, exist_ok=True)

    file_handler = logging.FileHandler(config.LOG_FILE)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    return logger