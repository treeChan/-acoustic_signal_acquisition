"""Launch the external updater after all acquisition files are safely closed."""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .errors import UpdateError


HELPER_NAMES = ("AcousticUpdater.exe", "AcousticUpdater")


@dataclass(frozen=True)
class InstallPlan:
    command: tuple[str, ...]
    target: Path
    status_file: Path


def current_macos_app_path(executable: Path | None = None) -> Path:
    path = (executable or Path(sys.executable)).resolve()
    for parent in (path, *path.parents):
        if parent.suffix == ".app":
            return parent
    raise UpdateError(
        "install_target", "当前程序不在 macOS .app 中，不能执行自动覆盖安装。"
    )


def _helper_command() -> tuple[str, ...]:
    if not getattr(sys, "frozen", False):
        return (sys.executable, "-m", "updater.helper")
    roots = [Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)), Path(sys.executable).parent]
    for root in roots:
        for name in HELPER_NAMES:
            direct = root / name
            if direct.is_file():
                return (str(direct),)
            try:
                match = next(root.rglob(name))
            except (OSError, StopIteration):
                continue
            if match.is_file():
                return (str(match),)
    raise UpdateError("installer_missing", "外部更新助手缺失，请重新安装完整应用。")


def _status_file() -> Path:
    if platform.system() == "Windows":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path.home() / "Library" / "Caches"
    result = base / "AcousticVectorAcquisition" / "updater-status.json"
    result.parent.mkdir(parents=True, exist_ok=True)
    return result


def build_install_plan(
    package_path: Path,
    expected_sha256: str,
    *,
    platform_key: str,
    executable: Path | None = None,
    parent_pid: int | None = None,
    status_file: Path | None = None,
) -> InstallPlan:
    package = package_path.resolve()
    if not package.is_file():
        raise UpdateError("installer_package", "已验证的更新安装包不存在。")
    helper = _helper_command()
    pid = parent_pid or os.getpid()
    # Tests and managed deployments can supply an isolated status location.
    # The default remains per-user and deliberately outside every recording
    # directory and the application installation directory.
    status = status_file.resolve() if status_file is not None else _status_file()
    status.parent.mkdir(parents=True, exist_ok=True)
    common = (
        *helper,
        "--platform",
        platform_key,
        "--package",
        str(package),
        "--parent-pid",
        str(pid),
        "--sha256",
        expected_sha256.lower(),
        "--status-file",
        str(status),
    )
    if platform_key == "macos-arm64":
        target = current_macos_app_path(executable)
        parent = target.parent
        if not os.access(parent, os.W_OK):
            raise UpdateError(
                "install_permission",
                f"没有权限更新 {target}。",
                "请使用有权限的账户安装，或先把应用移到当前用户可写的 Applications 目录。",
            )
        if package.suffix.lower() != ".zip":
            raise UpdateError("installer_package", "macOS 自动更新包必须是签名、公证后的 ZIP。")
        command = (*common, "--target", str(target), "--relaunch")
        return InstallPlan(command, target, status)
    if platform_key == "windows-x64":
        target = Path(executable or sys.executable).resolve()
        if package.suffix.lower() != ".exe":
            raise UpdateError("installer_package", "Windows 自动更新包必须是 setup.exe。")
        command = (*common, "--target", str(target), "--relaunch")
        return InstallPlan(command, target, status)
    raise UpdateError("unsupported_platform", f"不支持安装平台：{platform_key}")


def launch_install_plan(plan: InstallPlan, *, test_mode: bool = False) -> None:
    if test_mode:
        return
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(plan.command, **kwargs)
    except OSError as exc:
        raise UpdateError("installer_launch", "无法启动外部更新助手。") from exc
