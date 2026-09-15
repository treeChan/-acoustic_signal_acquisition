"""Standalone external updater helper built as a separate executable."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


BUNDLE_ID = "com.chenshu.acousticvector"


def _write_status(path: Path, state: str, message: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".part")
    temporary.write_text(
        json.dumps(
            {
                "state": state,
                "message": message,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def _wait_for_exit(pid: int, timeout: float = 120.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        time.sleep(0.2)
    raise RuntimeError("主程序在 120 秒内没有安全退出，更新已取消。")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_extract(zip_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(zip_path) as archive:
        for info in archive.infolist():
            relative = PurePosixPath(info.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise RuntimeError("更新 ZIP 包含不安全路径。")
            unix_mode = (info.external_attr >> 16) & 0o170000
            if unix_mode not in {0, 0o040000, 0o100000, 0o120000}:
                raise RuntimeError("更新 ZIP 包含不允许的特殊文件。")
            if unix_mode == 0o120000:
                try:
                    link_target = PurePosixPath(archive.read(info).decode("utf-8"))
                except UnicodeDecodeError as exc:
                    raise RuntimeError("更新 ZIP 包含无效符号链接。") from exc
                if link_target.is_absolute():
                    raise RuntimeError("更新 ZIP 的符号链接指向包外。")
                depth = 0
                for part in (*relative.parent.parts, *link_target.parts):
                    if part in {"", "."}:
                        continue
                    if part == "..":
                        depth -= 1
                    else:
                        depth += 1
                    if depth < 0:
                        raise RuntimeError("更新 ZIP 的符号链接指向包外。")
    completed = subprocess.run(
        ["/usr/bin/ditto", "-x", "-k", str(zip_path), str(destination)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode:
        raise RuntimeError("macOS 无法解压更新 ZIP。")
    root = destination.resolve()
    for directory, directories, files in os.walk(destination, followlinks=False):
        for name in (*directories, *files):
            candidate = Path(directory) / name
            if candidate.is_symlink() and not candidate.resolve(strict=False).is_relative_to(root):
                raise RuntimeError("更新 ZIP 解压后包含指向包外的符号链接。")


def _bundle_identifier(app_path: Path) -> str:
    plist_path = app_path / "Contents" / "Info.plist"
    with plist_path.open("rb") as handle:
        value = plistlib.load(handle).get("CFBundleIdentifier")
    return str(value or "")


def _find_app(root: Path) -> Path:
    apps = [path for path in root.rglob("*.app") if path.is_dir()]
    apps = [path for path in apps if not any(parent.suffix == ".app" for parent in path.parents)]
    if len(apps) != 1:
        raise RuntimeError(f"更新 ZIP 必须恰好包含一个 .app，实际找到 {len(apps)} 个。")
    if _bundle_identifier(apps[0]) != BUNDLE_ID:
        raise RuntimeError("更新包应用标识不匹配，拒绝安装。")
    return apps[0]


def _verify_macos_signature(app_path: Path) -> None:
    checks = (
        ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app_path)],
        ["/usr/sbin/spctl", "--assess", "--type", "execute", str(app_path)],
    )
    for command in checks:
        completed = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"macOS 系统签名验证失败：{Path(command[0]).name}")


def _install_macos(package: Path, target: Path, relaunch: bool) -> None:
    if target.suffix != ".app" or not target.exists():
        raise RuntimeError("目标 macOS 应用不存在或不是 .app。")
    if _bundle_identifier(target) != BUNDLE_ID:
        raise RuntimeError("当前应用标识不匹配，拒绝覆盖。")
    if not os.access(target.parent, os.W_OK):
        raise PermissionError(f"没有权限写入 {target.parent}")
    extract_root = Path(tempfile.mkdtemp(prefix="acoustic-update-extract-"))
    staged_target = target.parent / f".{target.name}.new-{uuid.uuid4().hex}"
    backup = target.parent / f".{target.name}.backup-{uuid.uuid4().hex}"
    try:
        _safe_extract(package, extract_root)
        source_app = _find_app(extract_root)
        _verify_macos_signature(source_app)
        copied = subprocess.run(
            ["/usr/bin/ditto", str(source_app), str(staged_target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if copied.returncode:
            raise RuntimeError("无法把更新应用暂存到安装目录。")
        _verify_macos_signature(staged_target)
        os.replace(target, backup)
        try:
            os.replace(staged_target, target)
        except Exception:
            os.replace(backup, target)
            raise
        shutil.rmtree(backup, ignore_errors=True)
        if relaunch:
            subprocess.Popen(
                ["/usr/bin/open", str(target)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
    finally:
        shutil.rmtree(extract_root, ignore_errors=True)
        shutil.rmtree(staged_target, ignore_errors=True)


def _install_windows(package: Path, relaunch: bool) -> None:
    arguments = [
        str(package),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CLOSEAPPLICATIONS",
    ]
    if relaunch:
        arguments.append("/RESTARTAPPLICATIONS")
    completed = subprocess.run(
        arguments,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        check=False,
    )
    if completed.returncode not in {0, 3010}:
        raise RuntimeError(f"Windows 安装器退出码：{completed.returncode}")


def _cleanup_download(package: Path) -> None:
    """Remove only updater-owned temporary downloads, never user-selected paths."""
    parent = package.parent
    if parent.name.startswith("acoustic-update-"):
        shutil.rmtree(parent, ignore_errors=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=("macos-arm64", "windows-x64"))
    parser.add_argument("--package", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--parent-pid", required=True, type=int)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--status-file", required=True, type=Path)
    parser.add_argument("--relaunch", action="store_true")
    parser.add_argument("--test-mode", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    should_cleanup = (
        not args.test_mode and args.package.parent.name.startswith("acoustic-update-")
    )
    try:
        if args.test_mode:
            _write_status(args.status_file, "test", "测试模式：未修改任何应用或用户数据。")
            return 0
        _wait_for_exit(args.parent_pid)
        if not args.package.is_file():
            raise RuntimeError("更新安装包不存在。")
        if _sha256(args.package).lower() != args.sha256.lower():
            raise RuntimeError("外部更新助手复核 SHA-256 失败。")
        _write_status(args.status_file, "installing", "正在安装更新。")
        if args.platform == "macos-arm64":
            _install_macos(args.package, args.target, args.relaunch)
        else:
            _install_windows(args.package, args.relaunch)
        _write_status(args.status_file, "installed", "更新安装程序已成功启动。")
        return 0
    except Exception as exc:
        _write_status(args.status_file, "failed", f"{type(exc).__name__}: {exc}")
        return 1
    finally:
        if should_cleanup:
            _cleanup_download(args.package)


if __name__ == "__main__":
    raise SystemExit(main())
