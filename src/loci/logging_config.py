"""Configures loci logging — stderr for interactive runs, file for daemons."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_FMT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(data_dir: Path, *, verbose: bool = False) -> None:
    """Wire root logger to stderr + a rotating file under data_dir/logs/.

    Safe to call multiple times (idempotent via handler-name guard).
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    _silence_noisy_libs()

    if _already_configured(root):
        return

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(logging.Formatter(_FMT))
    stderr_handler.name = "loci_stderr"
    root.addHandler(stderr_handler)

    logs_dir = data_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "loci.log",
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(_FMT))
    file_handler.name = "loci_file"
    root.addHandler(file_handler)


def _silence_noisy_libs() -> None:
    logging.getLogger("anthropic").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


def _already_configured(root: logging.Logger) -> bool:
    return any(getattr(h, "name", "") in ("loci_stderr", "loci_file") for h in root.handlers)
