"""User data layout versioning for ~/.loci/.

Writes version.json to the data_dir on first run and performs one-shot
migrations when the on-disk layout is older than CURRENT_LAYOUT_VERSION.
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)

CURRENT_LAYOUT_VERSION = 1


def current_layout_version(data_dir: Path) -> int:
    version_file = data_dir / "version.json"
    if not version_file.exists():
        return 0
    try:
        return json.loads(version_file.read_text()).get("layout_version", 0)
    except Exception:
        return 0


def ensure_layout(data_dir: Path, assets_dir: Path) -> None:
    """Ensure data_dir is at CURRENT_LAYOUT_VERSION. Safe to call repeatedly."""
    version_file = data_dir / "version.json"
    current = current_layout_version(data_dir)
    if current < CURRENT_LAYOUT_VERSION:
        _migrate(current, CURRENT_LAYOUT_VERSION, data_dir, assets_dir)
        version_file.write_text(json.dumps({"layout_version": CURRENT_LAYOUT_VERSION}))
    elif not version_file.exists():
        version_file.write_text(json.dumps({"layout_version": CURRENT_LAYOUT_VERSION}))


def _migrate(from_v: int, to_v: int, data_dir: Path, assets_dir: Path) -> None:
    if from_v < 1 <= to_v:
        _migrate_0_to_1(data_dir, assets_dir)


def _migrate_0_to_1(data_dir: Path, assets_dir: Path) -> None:
    """Move d3.v7.min.js from data_dir root into assets/."""
    old = data_dir / "d3.v7.min.js"
    new = assets_dir / "d3.v7.min.js"
    if old.exists() and not new.exists():
        assets_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(new))
        log.info("layout: migrated d3.v7.min.js to assets/")
