#!/usr/bin/env python3
"""Create and Ed25519-sign update.json without exposing the private key."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.sync_version import read_version  # noqa: E402


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def artifact_payload(version: str, platform_key: str, item: dict[str, object]) -> bytes:
    descriptor = {
        "version": version,
        "platform": platform_key,
        "url": item["url"],
        "size": item["size"],
        "sha256": item["sha256"],
    }
    return b"acoustic-vector-update-artifact-v1\0" + canonical_json(descriptor)


def manifest_payload(value: dict[str, object]) -> bytes:
    unsigned = dict(value)
    unsigned.pop("manifest_signature", None)
    return b"acoustic-vector-update-manifest-v1\0" + canonical_json(unsigned)


def load_private_key(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError:
        try:
            key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(data.strip(), validate=True))
        except Exception as exc:
            raise SystemExit("Release private key is neither Ed25519 PEM nor Base64 raw key") from exc
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Release private key is not Ed25519")
    return key


def parse_artifact(value: str) -> tuple[str, Path]:
    try:
        platform_key, path = value.split("=", 1)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("artifact must be PLATFORM=PATH") from exc
    if platform_key not in {"macos-arm64", "windows-x64"}:
        raise argparse.ArgumentTypeError(f"unsupported platform: {platform_key}")
    return platform_key, Path(path)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--notes", required=True, type=Path)
    parser.add_argument("--base-download-url", required=True)
    parser.add_argument("--artifact", action="append", type=parse_artifact, required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--metadata-dir", type=Path)
    args = parser.parse_args()

    version = read_version()
    key = load_private_key(args.private_key)
    platforms: dict[str, dict[str, object]] = {}
    for platform_key, path in args.artifact:
        if platform_key in platforms:
            raise SystemExit(f"Duplicate artifact platform: {platform_key}")
        if not path.is_file():
            raise SystemExit(f"Artifact does not exist: {path}")
        item: dict[str, object] = {
            "url": args.base_download_url.rstrip("/") + "/" + quote(path.name),
            "size": path.stat().st_size,
            "sha256": sha256(path),
            "signature": "",
        }
        item["signature"] = base64.b64encode(
            key.sign(artifact_payload(version, platform_key, item))
        ).decode("ascii")
        platforms[platform_key] = item
        if args.metadata_dir:
            args.metadata_dir.mkdir(parents=True, exist_ok=True)
            (args.metadata_dir / f"{path.name}.sha256").write_text(
                f"{item['sha256']}  {path.name}\n", encoding="utf-8"
            )
            (args.metadata_dir / f"{path.name}.sig").write_text(
                str(item["signature"]) + "\n", encoding="ascii"
            )
    if set(platforms) != {"macos-arm64", "windows-x64"}:
        raise SystemExit("Both macos-arm64 and windows-x64 artifacts are required")
    manifest: dict[str, object] = {
        "version": version,
        "notes": args.notes.read_text(encoding="utf-8"),
        "published_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
        "manifest_signature": "",
    }
    manifest["manifest_signature"] = base64.b64encode(
        key.sign(manifest_payload(manifest))
    ).decode("ascii")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Created signed manifest: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
