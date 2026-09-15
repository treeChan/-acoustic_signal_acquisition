#!/usr/bin/env python3
"""Validate VERSION and generate platform build metadata from that single source."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from packaging.version import Version


ROOT = Path(__file__).resolve().parents[1]
STABLE_RE = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PREVIEW_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-preview\."
    r"(1[0-2]|[1-9])([0-3]\d)([0-2]\d)([0-5]\d)$"
)


def read_version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not (STABLE_RE.fullmatch(value) or PREVIEW_RE.fullmatch(value)):
        raise SystemExit(f"Invalid VERSION: {value!r}")
    Version(value)
    return value


def numeric_version(value: str) -> tuple[int, int, int, int]:
    core = value.split("-", 1)[0]
    major, minor, patch = (int(item) for item in core.split("."))
    return major, minor, patch, 0


def windows_version_info(value: str) -> str:
    version_tuple = numeric_version(value)
    dotted = ".".join(str(item) for item in version_tuple)
    return f"""# UTF-8
VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={version_tuple!r}, prodvers={version_tuple!r},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('080404B0', [
      StringStruct('CompanyName', 'treeChan'),
      StringStruct('FileDescription', 'Acoustic Vector Acquisition System'),
      StringStruct('FileVersion', {value!r}),
      StringStruct('InternalName', 'AcousticVectorAcquisition'),
      StringStruct('OriginalFilename', 'AcousticVectorAcquisition.exe'),
      StringStruct('ProductName', 'Acoustic Vector Acquisition System'),
      StringStruct('ProductVersion', {value!r})])]),
    VarFileInfo([VarStruct('Translation', [2052, 1200])])])
# Numeric Windows version: {dotted}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--windows-version-file", type=Path)
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    value = read_version()
    numeric = ".".join(str(item) for item in numeric_version(value))
    channel = "preview" if "-preview." in value else "stable"
    tag = "preview" if channel == "preview" else f"v{value}"
    if args.windows_version_file:
        args.windows_version_file.parent.mkdir(parents=True, exist_ok=True)
        args.windows_version_file.write_text(windows_version_info(value), encoding="utf-8")
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"version={value}\nchannel={channel}\ntag={tag}\nnumeric_version={numeric}\n")
    print(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
