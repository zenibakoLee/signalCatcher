from __future__ import annotations

import logging
import sys
from pathlib import Path


def setup_logging(log_dir: Path | None = None) -> None:
    if log_dir is None:
        log_dir = Path(__file__).resolve().parent.parent.parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]

    file_handler = logging.FileHandler(log_dir / "pipeline.log")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    handlers.append(file_handler)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
