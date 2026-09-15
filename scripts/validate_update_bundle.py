#!/usr/bin/env python3
"""Verify a local release candidate without installing or touching user data."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from updater.models import decode_manifest, parse_manifest  # noqa: E402
from updater.verifier import (  # noqa: E402
    verify_artifact_signature,
    verify_file_hash,
    verify_manifest_signature,
)


def validate_bundle(
    asset_dir: Path, public_key_path: Path, *, expected_version: str | None = None
) -> None:
    raw = decode_manifest((asset_dir / "update.json").read_bytes())
    public_key = public_key_path.read_bytes()
    verify_manifest_signature(raw, public_key)
    manifest = parse_manifest(raw)
    if expected_version is not None and manifest.version != expected_version:
        raise ValueError(
            f"manifest version {manifest.version!r} != expected {expected_version!r}"
        )
    if set(manifest.platforms) != {"macos-arm64", "windows-x64"}:
        raise ValueError("manifest must contain exactly macos-arm64 and windows-x64")
    for platform_key, artifact in manifest.platforms.items():
        filename = Path(unquote(urlsplit(artifact.url).path)).name
        filename_is_unsigned = "-unsigned" in Path(filename).stem
        if filename_is_unsigned != (artifact.system_signature == "unsigned"):
            raise ValueError(f"filename/signature label mismatch for {platform_key}")
        package = asset_dir / filename
        if not package.is_file():
            raise FileNotFoundError(f"missing release asset for {platform_key}: {filename}")
        if package.stat().st_size != artifact.size:
            raise ValueError(f"size mismatch for {platform_key}")
        verify_file_hash(package, artifact.sha256)
        verify_artifact_signature(manifest.version, platform_key, artifact, public_key)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-dir", required=True, type=Path)
    parser.add_argument(
        "--public-key", type=Path, default=ROOT / "updater" / "public_key.pem"
    )
    parser.add_argument("--expected-version")
    args = parser.parse_args()
    validate_bundle(
        args.asset_dir, args.public_key, expected_version=args.expected_version
    )
    print("Verified local update bundle: manifest, sizes, SHA-256 and Ed25519 signatures")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
