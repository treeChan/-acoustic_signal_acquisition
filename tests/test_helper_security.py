from __future__ import annotations

import plistlib
import stat
import sys
import zipfile

import pytest

from updater import helper
from updater.helper import _safe_extract


def test_zip_path_traversal_is_rejected(tmp_path) -> None:
    package = tmp_path / "traversal.zip"
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr("../outside.txt", b"forbidden")
    with pytest.raises(RuntimeError, match="不安全路径"):
        _safe_extract(package, tmp_path / "extract")
    assert not (tmp_path / "outside.txt").exists()


def test_zip_escaping_symlink_is_rejected_before_extraction(tmp_path) -> None:
    package = tmp_path / "symlink.zip"
    link = zipfile.ZipInfo("Application.app/Contents/escape")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with zipfile.ZipFile(package, "w") as archive:
        archive.writestr(link, "../../../outside")
    with pytest.raises(RuntimeError, match="符号链接指向包外"):
        _safe_extract(package, tmp_path / "extract")
    assert not (tmp_path / "outside").exists()


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS updater uses ditto")
def test_unsigned_macos_update_replaces_only_app_and_preserves_user_data(
    tmp_path, monkeypatch
) -> None:
    def make_app(path, marker: bytes) -> None:
        contents = path / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        with (contents / "Info.plist").open("wb") as handle:
            plistlib.dump({"CFBundleIdentifier": helper.BUNDLE_ID}, handle)
        (contents / "MacOS" / "AcousticVectorAcquisition").write_bytes(marker)

    target = tmp_path / "Applications" / "AcousticVectorAcquisition.app"
    make_app(target, b"old")
    source = tmp_path / "package" / "AcousticVectorAcquisition.app"
    make_app(source, b"new")
    package = tmp_path / "update.zip"
    with zipfile.ZipFile(package, "w") as archive:
        for path in source.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(source.parent))
    records = tmp_path / "Documents" / "AcousticVectorRecords"
    records.mkdir(parents=True)
    user_files = {
        "capture.h5": b"h5-data",
        "capture.wav": b"wav-data",
        "capture.json": b"json-data",
        "manifest.jsonl": b"manifest-data",
        "user-config.json": b"config-data",
    }
    for name, value in user_files.items():
        (records / name).write_bytes(value)

    def must_not_verify(_path) -> None:
        raise AssertionError("unsigned package must not be presented as Developer ID signed")

    monkeypatch.setattr(helper, "_verify_macos_signature", must_not_verify)
    helper._install_macos(package, target, False, "unsigned")

    assert (target / "Contents" / "MacOS" / "AcousticVectorAcquisition").read_bytes() == b"new"
    for name, value in user_files.items():
        assert (records / name).read_bytes() == value
