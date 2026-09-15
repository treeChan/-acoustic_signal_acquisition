"""Cancellable, streaming and verified update downloads."""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlsplit

from .checker import UpdateCheckResult
from .errors import UpdateCancelled, UpdateError
from .http_client import HttpClient
from .verifier import verify_artifact_signature, verify_file_hash


@dataclass(frozen=True)
class DownloadProgress:
    downloaded: int
    total: int
    bytes_per_second: float
    eta_seconds: float | None


@dataclass(frozen=True)
class DownloadResult:
    path: Path
    temporary_directory: Path

    def cleanup(self) -> None:
        shutil.rmtree(self.temporary_directory, ignore_errors=True)


def _safe_filename(url: str, platform_key: str) -> str:
    name = Path(unquote(urlsplit(url).path)).name
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        return "update.zip" if platform_key == "macos-arm64" else "update-setup.exe"
    return name


def download_and_verify(
    check: UpdateCheckResult,
    public_key_pem: bytes,
    *,
    client: HttpClient | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[DownloadProgress], None] | None = None,
    temporary_root: Path | None = None,
) -> DownloadResult:
    artifact = check.artifact
    http = client or HttpClient()
    cancel = cancel_event or threading.Event()
    temp_dir = Path(
        tempfile.mkdtemp(prefix="acoustic-update-", dir=str(temporary_root) if temporary_root else None)
    )
    final_path = temp_dir / _safe_filename(artifact.url, check.platform_key)
    part_path = final_path.with_name(final_path.name + ".part")
    response = None
    started = time.monotonic()
    last_progress = started
    downloaded = 0
    try:
        response = http.open(artifact.url, stream=True)
        header_length = response.headers.get("Content-Length")
        if header_length:
            try:
                announced = int(header_length)
            except ValueError as exc:
                raise UpdateError("download_size", "服务器返回了无效的 Content-Length。") from exc
            if announced != artifact.size:
                raise UpdateError(
                    "download_size",
                    "服务器文件大小与签名清单不一致。",
                    f"清单 {artifact.size} 字节，服务器 {announced} 字节。",
                )
        with part_path.open("xb") as output:
            try:
                for chunk in response.iter_content(chunk_size=256 * 1024):
                    now = time.monotonic()
                    if cancel.is_set():
                        raise UpdateCancelled()
                    if not chunk:
                        if now - last_progress > http.timeouts.no_progress:
                            raise UpdateError("no_progress", "下载 30 秒没有收到新数据，已停止。")
                        continue
                    try:
                        output.write(chunk)
                    except OSError as exc:
                        raise UpdateError(
                            "disk_write", "无法把更新安装包写入系统临时目录。", str(exc)
                        ) from exc
                    downloaded += len(chunk)
                    last_progress = now
                    if downloaded > artifact.size:
                        raise UpdateError("download_size", "下载数据超过清单声明大小，已停止。")
                    elapsed = max(now - started, 0.001)
                    speed = downloaded / elapsed
                    eta = (artifact.size - downloaded) / speed if speed > 0 else None
                    if progress_callback:
                        progress_callback(DownloadProgress(downloaded, artifact.size, speed, eta))
                    # A UI cancellation can be triggered by the progress signal,
                    # including when the whole file fits in this final chunk.
                    if cancel.is_set():
                        raise UpdateCancelled()
            except UpdateError:
                raise
            except Exception as exc:
                try:
                    http.raise_stream_error(exc, response.url or artifact.url, subject="安装包")
                except UpdateError as translated:
                    translated.detail = (
                        f"{translated.detail}\n" if translated.detail else ""
                    ) + f"已下载 {downloaded} 字节。"
                    raise
                raise AssertionError("unreachable")
            try:
                output.flush()
                os.fsync(output.fileno())
            except OSError as exc:
                raise UpdateError(
                    "disk_write", "无法把更新安装包完整写入磁盘。", str(exc)
                ) from exc
        if downloaded != artifact.size:
            raise UpdateError(
                "download_size",
                "安装包下载不完整。",
                f"清单 {artifact.size} 字节，实际 {downloaded} 字节。",
            )
        verify_file_hash(part_path, artifact.sha256)
        verify_artifact_signature(
            check.latest_version, check.platform_key, artifact, public_key_pem
        )
        os.replace(part_path, final_path)
        return DownloadResult(final_path, temp_dir)
    except Exception:
        try:
            part_path.unlink(missing_ok=True)
            final_path.unlink(missing_ok=True)
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)
        raise
    finally:
        if response is not None:
            response.close()
