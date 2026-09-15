"""Project version grammar and comparison helpers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from packaging.version import InvalidVersion, Version

from .errors import UpdateError


STABLE_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PREVIEW_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-preview\."
    r"(1[0-2]|[1-9])([0-3]\d)([0-2]\d)([0-5]\d)$"
)


@dataclass(frozen=True)
class ParsedProjectVersion:
    text: str
    parsed: Version
    channel: str


def parse_project_version(value: str) -> ParsedProjectVersion:
    value = value.strip()
    channel = "stable"
    match = PREVIEW_RE.fullmatch(value)
    if match:
        month, day, hour, minute = (int(part) for part in match.groups()[-4:])
        try:
            datetime(2000, month, day, hour, minute)
        except ValueError as exc:
            raise UpdateError("invalid_version", f"预览版本时间戳无效：{value}") from exc
        channel = "preview"
    elif not STABLE_RE.fullmatch(value):
        raise UpdateError(
            "invalid_version",
            f"版本号格式无效：{value}",
            "正式版应为 x.y.z；预览版应为 x.y.z-preview.MMDDHHMM。",
        )
    try:
        parsed = Version(value)
    except InvalidVersion as exc:
        raise UpdateError("invalid_version", f"版本号无法解析：{value}") from exc
    return ParsedProjectVersion(value, parsed, channel)


def is_newer(candidate: str, current: str) -> bool:
    """Compare with packaging.version.Version as required by the release contract."""
    return parse_project_version(candidate).parsed > parse_project_version(current).parsed


def preview_version(base_version: str, when: datetime) -> str:
    parsed = parse_project_version(base_version)
    if parsed.channel != "stable":
        raise UpdateError("invalid_version", "生成预览版本时，基础版本必须是正式版格式。")
    stamp = f"{when.month}{when.day:02d}{when.hour:02d}{when.minute:02d}"
    return f"{base_version}-preview.{stamp}"
