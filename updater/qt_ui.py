"""PyQt5 dialogs and background workers for secure desktop updates."""

from __future__ import annotations

import logging
import re
import sys
import threading
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from . import CURRENT_VERSION
from .checker import UpdateCheckResult, check_for_update
from .downloader import DownloadProgress, DownloadResult, download_and_verify
from .errors import UpdateCancelled, UpdateError
from .installer import build_install_plan, launch_install_plan


LOGGER = logging.getLogger("acoustic.updater")


def system_signature_label(system_signature: str) -> str:
    return {
        "apple-developer-id": "Apple Developer ID（已签名并公证）",
        "windows-authenticode": "Windows Authenticode（已签名）",
        "unsigned": "unsigned（无操作系统发行者签名）",
    }.get(system_signature, "未知")


def unsigned_platform_warning(platform_key: str, system_signature: str) -> str:
    if system_signature != "unsigned":
        return ""
    if platform_key == "macos-arm64":
        return (
            "此测试版没有 Apple Developer ID 签名或公证。首次手动运行时 macOS "
            "可能阻止打开；请确认来自本项目后，在 Finder 中右键应用并选择“打开”。"
            "程序仍会强制验证 Ed25519 和 SHA-256。"
        )
    if platform_key == "windows-x64":
        return (
            "此测试版没有 Windows Authenticode 签名。安装时可能显示 Microsoft Defender "
            "SmartScreen 或“未知发布者”提示；请仅在确认来自本项目后继续。"
            "程序仍会强制验证 Ed25519 和 SHA-256。"
        )
    return "此测试版没有操作系统发行者签名，请仅在确认来源后安装。"


def embedded_public_key() -> bytes:
    candidates: list[Path] = [Path(__file__).with_name("public_key.pem")]
    frozen_root = getattr(sys, "_MEIPASS", None)
    if frozen_root:
        root = Path(frozen_root)
        candidates[:0] = [root / "updater" / "public_key.pem", root / "public_key.pem"]
    for candidate in candidates:
        try:
            return candidate.read_bytes()
        except OSError:
            continue
    raise UpdateError(
        "public_key", "内置更新公钥缺失，无法安全检查更新。", "请重新安装官方版本。"
    )


def safe_markdown(markdown: str) -> str:
    """Keep basic formatting but remove active HTML, links and remote images."""
    value = markdown.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    value = re.sub(r"!\[([^\]]*)\]\([^\n)]*\)", r"[图片：\1]", value)
    value = re.sub(r"\[([^\]]+)\]\([^\n)]*\)", r"\1", value)
    return value


def format_bytes(value: float) -> str:
    units = ("B", "KiB", "MiB", "GiB")
    size = float(value)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} GiB"


class SafeNotesBrowser(QtWidgets.QTextBrowser):
    def loadResource(self, resource_type: int, name: QtCore.QUrl):  # noqa: N802
        if resource_type == QtGui.QTextDocument.ImageResource:
            return QtCore.QByteArray()
        return super().loadResource(resource_type, name)


class CheckWorker(QtCore.QObject):
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()

    @QtCore.pyqtSlot()
    def run(self) -> None:
        try:
            self.completed.emit(check_for_update(CURRENT_VERSION, embedded_public_key()))
        except UpdateError as exc:
            self.failed.emit(exc)
        except Exception as exc:
            self.failed.emit(UpdateError("internal", "检查更新时发生内部错误。", type(exc).__name__))
        finally:
            self.finished.emit()


class DownloadWorker(QtCore.QObject):
    progress = QtCore.pyqtSignal(object)
    completed = QtCore.pyqtSignal(object)
    failed = QtCore.pyqtSignal(object)
    finished = QtCore.pyqtSignal()

    def __init__(self, check: UpdateCheckResult) -> None:
        super().__init__()
        self.check = check
        self.cancel_event = threading.Event()

    @QtCore.pyqtSlot()
    def cancel(self) -> None:
        self.cancel_event.set()

    @QtCore.pyqtSlot()
    def run(self) -> None:
        try:
            result = download_and_verify(
                self.check,
                embedded_public_key(),
                cancel_event=self.cancel_event,
                progress_callback=self.progress.emit,
            )
            self.completed.emit(result)
        except (UpdateError, UpdateCancelled) as exc:
            self.failed.emit(exc)
        except Exception as exc:
            self.failed.emit(UpdateError("internal", "下载更新时发生内部错误。", type(exc).__name__))
        finally:
            self.finished.emit()


class UpdateAvailableDialog(QtWidgets.QDialog):
    def __init__(self, check: UpdateCheckResult, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("发现新版本")
        self.resize(640, 480)
        layout = QtWidgets.QVBoxLayout(self)
        heading = QtWidgets.QLabel("发现可用更新")
        heading.setStyleSheet("font-size: 20px; font-weight: 700;")
        layout.addWidget(heading)
        versions = QtWidgets.QLabel(
            f"当前版本：{check.current_version}\n"
            f"最新版本：{check.latest_version}\n"
            f"系统签名：{system_signature_label(check.artifact.system_signature)}\n"
            "完整性验证：Ed25519 + SHA-256（强制）"
        )
        versions.setTextInteractionFlags(QtCore.Qt.TextSelectableByMouse)
        layout.addWidget(versions)
        warning_text = unsigned_platform_warning(
            check.platform_key, check.artifact.system_signature
        )
        if warning_text:
            warning = QtWidgets.QLabel("⚠ " + warning_text)
            warning.setObjectName("unsignedUpdateWarning")
            warning.setWordWrap(True)
            warning.setStyleSheet(
                "background: #fff3cd; color: #664d03; border: 1px solid #ffecb5; "
                "border-radius: 5px; padding: 10px;"
            )
            layout.addWidget(warning)
        layout.addWidget(QtWidgets.QLabel("更新说明："))
        notes = SafeNotesBrowser()
        notes.setOpenExternalLinks(False)
        notes.setOpenLinks(False)
        notes.setMarkdown(safe_markdown(check.manifest.notes))
        layout.addWidget(notes, 1)
        buttons = QtWidgets.QDialogButtonBox()
        later = buttons.addButton("稍后更新", QtWidgets.QDialogButtonBox.RejectRole)
        update = buttons.addButton("立即更新", QtWidgets.QDialogButtonBox.AcceptRole)
        later.clicked.connect(self.reject)
        update.clicked.connect(self.accept)
        update.setDefault(True)
        layout.addWidget(buttons)


class DownloadDialog(QtWidgets.QDialog):
    cancel_requested = QtCore.pyqtSignal()

    def __init__(self, parent: QtWidgets.QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("下载更新")
        self.setModal(True)
        self.setMinimumWidth(520)
        layout = QtWidgets.QVBoxLayout(self)
        self.summary = QtWidgets.QLabel("正在准备下载…")
        layout.addWidget(self.summary)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 1000)
        layout.addWidget(self.progress)
        self.detail = QtWidgets.QLabel("已下载 0 B")
        layout.addWidget(self.detail)
        self.cancel_button = QtWidgets.QPushButton("取消")
        self.cancel_button.clicked.connect(self._request_cancel)
        layout.addWidget(self.cancel_button, alignment=QtCore.Qt.AlignRight)

    def _request_cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.summary.setText("正在取消并清理临时文件…")
        self.cancel_requested.emit()

    def reject(self) -> None:
        if self.cancel_button.isEnabled():
            self._request_cancel()

    @QtCore.pyqtSlot(object)
    def update_progress(self, progress: DownloadProgress) -> None:
        ratio = progress.downloaded / max(progress.total, 1)
        self.progress.setValue(min(1000, round(ratio * 1000)))
        speed = format_bytes(progress.bytes_per_second) + "/s"
        eta = "--"
        if progress.eta_seconds is not None:
            seconds = max(0, round(progress.eta_seconds))
            eta = f"{seconds // 60:02d}:{seconds % 60:02d}"
        self.summary.setText(f"正在下载… {ratio * 100:.1f}%")
        self.detail.setText(
            f"{format_bytes(progress.downloaded)} / {format_bytes(progress.total)}"
            f"    速度 {speed}    预计剩余 {eta}"
        )


class UpdateManager(QtCore.QObject):
    def __init__(self, window: QtWidgets.QMainWindow) -> None:
        super().__init__(window)
        self.window = window
        self.check_action: QtWidgets.QAction | None = None
        self._check_thread: QtCore.QThread | None = None
        self._check_worker: CheckWorker | None = None
        self._check_silent = False
        self._download_thread: QtCore.QThread | None = None
        self._download_worker: DownloadWorker | None = None
        self._download_dialog: DownloadDialog | None = None
        self._download_check: UpdateCheckResult | None = None

    def set_action(self, action: QtWidgets.QAction) -> None:
        self.check_action = action

    def check(self, *, silent: bool = False) -> None:
        if self._check_thread and self._check_thread.isRunning():
            if not silent:
                QtWidgets.QMessageBox.information(self.window, "检查更新", "更新检查正在进行。")
            return
        self._check_silent = silent
        if self.check_action:
            self.check_action.setEnabled(False)
        thread = QtCore.QThread(self)
        worker = CheckWorker()
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.completed.connect(self._check_completed)
        worker.failed.connect(self._check_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._check_finished)
        self._check_thread = thread
        self._check_worker = worker
        thread.start()

    @QtCore.pyqtSlot(object)
    def _check_completed(self, result: UpdateCheckResult) -> None:
        if not result.update_available:
            if not self._check_silent:
                QtWidgets.QMessageBox.information(
                    self.window,
                    "检查更新",
                    f"当前已是最新版本：{result.current_version}\n渠道：{result.channel}",
                )
            return
        dialog = UpdateAvailableDialog(result, self.window)
        if dialog.exec_() == QtWidgets.QDialog.Accepted:
            self._start_download(result)

    @QtCore.pyqtSlot(object)
    def _check_failed(self, error: UpdateError) -> None:
        LOGGER.warning("Update check failed [%s]: %s", error.code, error.message)
        if self._check_silent:
            status = getattr(self.window, "status_label", None)
            if status is not None:
                status.setToolTip(f"后台更新检查失败：{error.user_text()}")
            return
        QtWidgets.QMessageBox.critical(self.window, "检查更新失败", error.user_text())

    @QtCore.pyqtSlot()
    def _check_finished(self) -> None:
        if self.check_action:
            self.check_action.setEnabled(True)
        if self._check_thread:
            self._check_thread.deleteLater()
        self._check_thread = None
        self._check_worker = None

    def _start_download(self, check: UpdateCheckResult) -> None:
        if self._download_thread and self._download_thread.isRunning():
            return
        dialog = DownloadDialog(self.window)
        thread = QtCore.QThread(self)
        worker = DownloadWorker(check)
        worker.moveToThread(thread)
        dialog.cancel_requested.connect(worker.cancel, QtCore.Qt.DirectConnection)
        worker.progress.connect(dialog.update_progress)
        worker.completed.connect(self._download_completed)
        worker.failed.connect(self._download_failed)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._download_finished)
        self._download_check = check
        self._download_dialog = dialog
        self._download_thread = thread
        self._download_worker = worker
        dialog.show()
        thread.start()

    @QtCore.pyqtSlot(object)
    def _download_completed(self, result: DownloadResult) -> None:
        if self._download_dialog:
            self._download_dialog.accept()
        check = self._download_check
        if check is None:
            result.cleanup()
            return
        prepare = getattr(self.window, "prepare_for_update_install", None)
        if prepare is None:
            result.cleanup()
            QtWidgets.QMessageBox.critical(self.window, "无法安装", "主窗口缺少安全停机接口。")
            return
        prepare(result, check)

    @QtCore.pyqtSlot(object)
    def _download_failed(self, error: UpdateError) -> None:
        if self._download_dialog:
            self._download_dialog.accept()
        if error.code != "cancelled":
            QtWidgets.QMessageBox.critical(self.window, "更新下载失败", error.user_text())

    @QtCore.pyqtSlot()
    def _download_finished(self) -> None:
        if self._download_thread:
            self._download_thread.deleteLater()
        self._download_thread = None
        self._download_worker = None
        self._download_dialog = None

    def install_verified(self, result: DownloadResult, check: UpdateCheckResult) -> bool:
        try:
            plan = build_install_plan(
                result.path,
                check.artifact.sha256,
                platform_key=check.platform_key,
                system_signature=check.artifact.system_signature,
            )
            launch_install_plan(plan)
        except UpdateError as exc:
            result.cleanup()
            QtWidgets.QMessageBox.critical(self.window, "无法安装更新", exc.user_text())
            return False
        QtWidgets.QApplication.quit()
        return True
