from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from updater.checker import detect_platform
from updater.errors import UpdateError
from updater.installer import build_install_plan, launch_install_plan


def test_platform_detection() -> None:
    assert detect_platform("Darwin", "arm64") == "macos-arm64"
    assert detect_platform("Windows", "AMD64") == "windows-x64"
    with pytest.raises(UpdateError):
        detect_platform("Darwin", "x86_64")


def test_install_plan_test_mode_never_overwrites_app(tmp_path: Path) -> None:
    app = tmp_path / "AcousticVectorAcquisition.app"
    executable = app / "Contents" / "MacOS" / "AcousticVectorAcquisition"
    executable.parent.mkdir(parents=True)
    executable.write_bytes(b"original-app")
    package = tmp_path / "update.zip"
    package.write_bytes(b"not-used-in-plan-test")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    plan = build_install_plan(
        package,
        digest,
        platform_key="macos-arm64",
        system_signature="unsigned",
        executable=executable,
        parent_pid=12345,
        status_file=tmp_path / "updater-status.json",
    )
    launch_install_plan(plan, test_mode=True)
    assert executable.read_bytes() == b"original-app"
    assert package.exists()
    assert "--system-signature" in plan.command
    assert "unsigned" in plan.command
