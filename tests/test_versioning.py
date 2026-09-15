from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from scripts.sync_version import numeric_version
from updater.errors import UpdateError
from updater.versioning import is_newer, parse_project_version, preview_version


def test_stable_and_preview_version_grammar() -> None:
    assert parse_project_version("1.2.3").channel == "stable"
    preview = parse_project_version("1.2.3-preview.9150930")
    assert preview.channel == "preview"
    assert preview.parsed.is_prerelease


@pytest.mark.parametrize(
    "value",
    ["1.2", "01.2.3", "1.2.3-preview.09150930", "1.2.3-preview.13250930", "v1.2.3"],
)
def test_invalid_versions_are_rejected(value: str) -> None:
    with pytest.raises(UpdateError, match="版本号"):
        parse_project_version(value)


def test_comparison_uses_packaging_semantics() -> None:
    assert is_newer("0.2.0", "0.1.9")
    assert is_newer("0.2.0-preview.9150930", "0.1.9")
    assert not is_newer("0.2.0-preview.9150930", "0.2.0")
    assert not is_newer("0.2.0", "0.2.0")


def test_preview_timestamp_has_no_month_leading_zero() -> None:
    assert preview_version("1.4.0", datetime(2026, 9, 5, 7, 3)) == "1.4.0-preview.9050703"


def test_windows_installer_uses_numeric_file_version_for_preview() -> None:
    assert numeric_version("0.2.0-preview.9152209") == (0, 2, 0, 0)
    installer = (
        Path(__file__).resolve().parents[1]
        / "installer"
        / "windows"
        / "AcousticVectorAcquisition.iss"
    ).read_text(encoding="utf-8")
    assert "VersionInfoProductVersion={#MyNumericVersion}" in installer
