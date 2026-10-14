"""
modules/logger.py
-----------------
Centralized logging configuration for the Tableau Validation Framework.
Supports console (colorized) + rotating file logging.
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import colorlog


def setup_logger(name: str, log_level: str = "INFO", log_file: str = None) -> logging.Logger:
    """
    Configure and return a named logger with color console + optional file handler.

    Args:
        name:      Logger name (typically __name__ of calling module)
        log_level: String log level (DEBUG / INFO / WARNING / ERROR / CRITICAL)
        log_file:  Optional path to rotating log file

    Returns:
        Configured logging.Logger instance
    """
    level = getattr(logging, log_level.upper(), logging.INFO)

    logger = logging.getLogger(name)
    logger.setLevel(level)

    # Prevent duplicate handlers when module is re-imported
    if logger.handlers:
        return logger

    # ── Console handler (colorized) ──────────────────────────────────────────
    console_fmt = (
        "%(log_color)s%(asctime)s [%(levelname)-8s] %(name)s%(reset)s — %(message)s"
    )
    console_handler = colorlog.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(
        colorlog.ColoredFormatter(
            console_fmt,
            datefmt="%Y-%m-%d %H:%M:%S",
            log_colors={
                "DEBUG":    "cyan",
                "INFO":     "green",
                "WARNING":  "yellow",
                "ERROR":    "red",
                "CRITICAL": "bold_red",
            },
        )
    )
    logger.addHandler(console_handler)

    # ── File handler (rotating, plain-text) ──────────────────────────────────
    if log_file:
        os.makedirs(os.path.dirname(os.path.abspath(log_file)), exist_ok=True)
        file_fmt = "%(asctime)s [%(levelname)-8s] %(name)s — %(message)s"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=20 * 1024 * 1024,  # 20 MB
            backupCount=5,
            encoding="utf-8",
        )
        file_handler.setLevel(level)
        file_handler.setFormatter(logging.Formatter(file_fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)

    return logger
