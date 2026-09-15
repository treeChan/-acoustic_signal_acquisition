from __future__ import annotations

import stat
import zipfile

import pytest

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
