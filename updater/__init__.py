"""Secure updater for the acoustic vector acquisition desktop application."""

from __future__ import annotations

import sys
from pathlib import Path


def _version_candidates() -> list[Path]:
    candidates = [Path(__file__).resolve().parents[1] / "VERSION"]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        candidates.insert(0, Path(frozen_root) / "VERSION")
    executable = Path(sys.executable).resolve()
    candidates.extend(
        [
            executable.parent / "VERSION",
            executable.parent.parent / "Resources" / "VERSION",
        ]
    )
    return candidates


def read_current_version() -> str:
    for candidate in _version_candidates():
        try:
            value = candidate.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if value:
            return value
    raise RuntimeError("应用版本文件 VERSION 缺失；请重新安装完整应用。")


CURRENT_VERSION = read_current_version()

__all__ = ["CURRENT_VERSION", "read_current_version"]
