"""Fetch and validate update manifests without Qt dependencies."""

from __future__ import annotations

import platform
from dataclasses import dataclass

from .errors import UpdateError
from .http_client import HttpClient
from .models import UpdateArtifact, UpdateManifest, decode_manifest, parse_manifest
from .verifier import verify_manifest_signature
from .versioning import is_newer, parse_project_version


REPOSITORY = "treeChan/-acoustic_signal_acquisition"
STABLE_MANIFEST_URL = (
    f"https://github.com/{REPOSITORY}/releases/latest/download/update.json"
)
PREVIEW_MANIFEST_URL = (
    f"https://github.com/{REPOSITORY}/releases/download/preview/update.json"
)


@dataclass(frozen=True)
class UpdateCheckResult:
    current_version: str
    latest_version: str
    channel: str
    platform_key: str
    update_available: bool
    manifest: UpdateManifest
    artifact: UpdateArtifact


def detect_platform(system: str | None = None, machine: str | None = None) -> str:
    system_value = (system or platform.system()).lower()
    machine_value = (machine or platform.machine()).lower()
    if system_value == "darwin" and machine_value in {"arm64", "aarch64"}:
        return "macos-arm64"
    if system_value == "windows" and machine_value in {"amd64", "x86_64"}:
        return "windows-x64"
    raise UpdateError(
        "unsupported_platform",
        f"当前平台不支持自动更新：{system_value}/{machine_value}",
        "仅支持 macOS Apple Silicon 和 Windows x64。",
    )


def manifest_url_for_channel(channel: str) -> str:
    if channel == "stable":
        return STABLE_MANIFEST_URL
    if channel == "preview":
        return PREVIEW_MANIFEST_URL
    raise UpdateError("channel", f"未知更新渠道：{channel}")


def fetch_manifest(
    url: str,
    public_key_pem: bytes,
    *,
    client: HttpClient | None = None,
    allow_localhost: bool = False,
) -> UpdateManifest:
    http = client or HttpClient(allow_localhost=allow_localhost)
    response = http.open(url, stream=True)
    chunks: list[bytes] = []
    total = 0
    try:
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > 1024 * 1024:
                raise UpdateError("manifest_json", "更新清单超过 1 MiB 安全限制。")
            chunks.append(chunk)
    except UpdateError:
        raise
    except Exception as exc:
        http.raise_stream_error(exc, response.url or url, subject="更新清单")
        raise AssertionError("unreachable")
    finally:
        response.close()
    raw = decode_manifest(b"".join(chunks))
    verify_manifest_signature(raw, public_key_pem)
    return parse_manifest(raw, allow_localhost=allow_localhost)


def check_for_update(
    current_version: str,
    public_key_pem: bytes,
    *,
    channel: str | None = None,
    platform_key: str | None = None,
    manifest_url: str | None = None,
    client: HttpClient | None = None,
    allow_localhost: bool = False,
) -> UpdateCheckResult:
    current = parse_project_version(current_version)
    selected_channel = channel or current.channel
    if selected_channel not in {"stable", "preview"}:
        raise UpdateError("channel", f"未知更新渠道：{selected_channel}")
    manifest = fetch_manifest(
        manifest_url or manifest_url_for_channel(selected_channel),
        public_key_pem,
        client=client,
        allow_localhost=allow_localhost,
    )
    if manifest.parsed_version.channel != selected_channel:
        raise UpdateError(
            "channel",
            f"更新清单版本 {manifest.version} 不属于 {selected_channel} 渠道。",
        )
    selected_platform = platform_key or detect_platform()
    artifact = manifest.platforms.get(selected_platform)
    if artifact is None:
        raise UpdateError(
            "platform_missing", f"更新清单缺少当前平台：{selected_platform}。"
        )
    return UpdateCheckResult(
        current_version=current.text,
        latest_version=manifest.version,
        channel=selected_channel,
        platform_key=selected_platform,
        update_available=is_newer(manifest.version, current.text),
        manifest=manifest,
        artifact=artifact,
    )
