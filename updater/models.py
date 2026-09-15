"""Strict update manifest models and validation."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from .errors import UpdateError, safe_url
from .versioning import ParsedProjectVersion, parse_project_version


MAX_MANIFEST_BYTES = 1024 * 1024
MAX_NOTES_LENGTH = 100_000
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
PLATFORM_KEYS = frozenset({"macos-arm64", "windows-x64"})


@dataclass(frozen=True)
class UpdateArtifact:
    url: str
    size: int
    sha256: str
    signature: str
    system_signature: str

    @property
    def is_system_signed(self) -> bool:
        return self.system_signature != "unsigned"


@dataclass(frozen=True)
class UpdateManifest:
    version: str
    parsed_version: ParsedProjectVersion
    notes: str
    published_at: datetime
    platforms: dict[str, UpdateArtifact]
    manifest_signature: str
    raw: dict[str, Any]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("manifest_json", f"更新清单包含重复字段：{key}")
        result[key] = value
    return result


def decode_manifest(payload: bytes) -> dict[str, Any]:
    if not payload:
        raise UpdateError("manifest_json", "更新清单为空。")
    if len(payload) > MAX_MANIFEST_BYTES:
        raise UpdateError("manifest_json", "更新清单超过 1 MiB 安全限制。")
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UpdateError("manifest_json", "更新清单不是有效的 UTF-8。") from exc
    try:
        value = json.loads(text, object_pairs_hook=_reject_duplicate_keys)
    except UpdateError:
        raise
    except json.JSONDecodeError as exc:
        raise UpdateError(
            "manifest_json", "更新清单 JSON 解析失败。", f"第 {exc.lineno} 行，第 {exc.colno} 列。"
        ) from exc
    if not isinstance(value, dict):
        raise UpdateError("manifest_json", "更新清单顶层必须是 JSON 对象。")
    return value


def validate_download_url(url: str, *, allow_localhost: bool = False) -> None:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise UpdateError("invalid_url", "更新下载地址格式无效。", safe_url(url)) from exc
    if parsed.username or parsed.password:
        raise UpdateError("invalid_url", "更新地址不能包含用户名或密码。", safe_url(url))
    if not parsed.hostname or not parsed.path:
        raise UpdateError("invalid_url", "更新下载地址不完整。", safe_url(url))
    del port
    is_local = parsed.hostname.lower() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme != "https" and not (allow_localhost and is_local and parsed.scheme == "http"):
        raise UpdateError("invalid_url", "更新只允许使用 HTTPS 下载地址。", safe_url(url))


def _valid_signature(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise UpdateError("manifest_schema", f"{field} 必须是 Base64 字符串。")
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise UpdateError("manifest_schema", f"{field} 不是有效的 Base64。") from exc
    if len(decoded) != 64:
        raise UpdateError("manifest_schema", f"{field} 必须是 64 字节 Ed25519 签名。")
    return value


def parse_manifest(raw: dict[str, Any], *, allow_localhost: bool = False) -> UpdateManifest:
    required = {"version", "notes", "published_at", "platforms", "manifest_signature"}
    missing = sorted(required - raw.keys())
    if missing:
        raise UpdateError("manifest_schema", f"更新清单缺少字段：{', '.join(missing)}")
    unknown = sorted(raw.keys() - required)
    if unknown:
        raise UpdateError("manifest_schema", f"更新清单包含不支持的字段：{', '.join(unknown)}")

    if not isinstance(raw["version"], str):
        raise UpdateError("manifest_schema", "version 必须是字符串。")
    parsed_version = parse_project_version(raw["version"])
    notes = raw["notes"]
    if not isinstance(notes, str) or len(notes) > MAX_NOTES_LENGTH:
        raise UpdateError("manifest_schema", "notes 必须是长度不超过 100000 的字符串。")
    if not isinstance(raw["published_at"], str):
        raise UpdateError("manifest_schema", "published_at 必须是 ISO-8601 字符串。")
    try:
        published_at = datetime.fromisoformat(raw["published_at"].replace("Z", "+00:00"))
    except ValueError as exc:
        raise UpdateError("manifest_schema", "published_at 不是有效的 ISO-8601 时间。") from exc
    if published_at.tzinfo is None:
        raise UpdateError("manifest_schema", "published_at 必须包含时区。")

    platform_values = raw["platforms"]
    if not isinstance(platform_values, dict) or not platform_values:
        raise UpdateError("manifest_schema", "platforms 必须是非空对象。")
    unknown_platforms = sorted(platform_values.keys() - PLATFORM_KEYS)
    if unknown_platforms:
        raise UpdateError("manifest_schema", f"清单包含未知平台：{', '.join(unknown_platforms)}")
    platforms: dict[str, UpdateArtifact] = {}
    for platform_key, value in platform_values.items():
        if not isinstance(value, dict):
            raise UpdateError("manifest_schema", f"平台 {platform_key} 的配置必须是对象。")
        artifact_fields = {"url", "size", "sha256", "signature", "system_signature"}
        if set(value) != artifact_fields:
            missing_artifact = sorted(artifact_fields - value.keys())
            extra_artifact = sorted(value.keys() - artifact_fields)
            detail = []
            if missing_artifact:
                detail.append(f"缺少 {', '.join(missing_artifact)}")
            if extra_artifact:
                detail.append(f"未知 {', '.join(extra_artifact)}")
            raise UpdateError("manifest_schema", f"平台 {platform_key} 字段错误：{'；'.join(detail)}")
        url = value["url"]
        if not isinstance(url, str):
            raise UpdateError("manifest_schema", f"平台 {platform_key} 的 url 必须是字符串。")
        validate_download_url(url, allow_localhost=allow_localhost)
        size = value["size"]
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise UpdateError("manifest_schema", f"平台 {platform_key} 的 size 必须是正整数。")
        sha256 = value["sha256"]
        if not isinstance(sha256, str) or not SHA256_RE.fullmatch(sha256):
            raise UpdateError("manifest_schema", f"平台 {platform_key} 的 sha256 无效。")
        signature = _valid_signature(value["signature"], f"platforms.{platform_key}.signature")
        system_signature = value["system_signature"]
        allowed_system_signatures = {
            "macos-arm64": {"apple-developer-id", "unsigned"},
            "windows-x64": {"windows-authenticode", "unsigned"},
        }[platform_key]
        if not isinstance(system_signature, str) or system_signature not in allowed_system_signatures:
            allowed = ", ".join(sorted(allowed_system_signatures))
            raise UpdateError(
                "manifest_schema",
                f"平台 {platform_key} 的 system_signature 必须是：{allowed}。",
            )
        platforms[platform_key] = UpdateArtifact(
            url, size, sha256.lower(), signature, system_signature
        )

    manifest_signature = _valid_signature(raw["manifest_signature"], "manifest_signature")
    return UpdateManifest(
        version=raw["version"],
        parsed_version=parsed_version,
        notes=notes,
        published_at=published_at,
        platforms=platforms,
        manifest_signature=manifest_signature,
        raw=raw,
    )
