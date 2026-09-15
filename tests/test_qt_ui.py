from __future__ import annotations

import os
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import QtCore, QtWidgets

from updater.errors import UpdateError
from updater.qt_ui import (
    UpdateManager,
    safe_markdown,
    system_signature_label,
    unsigned_platform_warning,
)


def test_release_notes_remove_html_links_and_remote_images() -> None:
    rendered = safe_markdown(
        "## 标题\n<script>alert(1)</script>\n[链接](https://evil.test/x)\n"
        "![像素](https://evil.test/pixel.png)"
    )
    assert "<script>" not in rendered
    assert "https://evil.test" not in rendered
    assert "链接" in rendered
    assert "[图片：像素]" in rendered


def test_unsigned_platform_warnings_are_explicit() -> None:
    assert "右键" in unsigned_platform_warning("macos-arm64", "unsigned")
    assert "Apple Developer ID" in unsigned_platform_warning("macos-arm64", "unsigned")
    assert "SmartScreen" in unsigned_platform_warning("windows-x64", "unsigned")
    assert "未知发布者" in unsigned_platform_warning("windows-x64", "unsigned")
    assert unsigned_platform_warning("macos-arm64", "apple-developer-id") == ""
    assert "unsigned" in system_signature_label("unsigned")
    assert "Apple Developer ID" in system_signature_label("apple-developer-id")


def test_update_check_does_not_block_qt_event_loop(monkeypatch) -> None:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    window = QtWidgets.QMainWindow()
    window.status_label = QtWidgets.QLabel(window)  # type: ignore[attr-defined]
    manager = UpdateManager(window)
    timer_fired = []

    def slow_check(*_args, **_kwargs):
        time.sleep(0.2)
        raise UpdateError("test", "expected test failure")

    monkeypatch.setattr("updater.qt_ui.check_for_update", slow_check)
    QtCore.QTimer.singleShot(20, lambda: timer_fired.append(True))
    started = time.monotonic()
    manager.check(silent=True)
    assert time.monotonic() - started < 0.1

    deadline = time.monotonic() + 2.0
    while manager._check_thread is not None and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert timer_fired
    assert manager._check_thread is None
    window.close()
