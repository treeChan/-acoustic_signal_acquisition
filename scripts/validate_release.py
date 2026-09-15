#!/usr/bin/env python3
"""End-to-end verification that a GitHub Release is actually updateable."""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

import requests


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from updater.models import decode_manifest, parse_manifest  # noqa: E402
from updater.verifier import verify_artifact_signature, verify_manifest_signature  # noqa: E402


def request(session: requests.Session, method: str, url: str, **kwargs: object) -> requests.Response:
    response = session.request(method, url, timeout=(10, 30), allow_redirects=True, **kwargs)
    if response.status_code < 200 or response.status_code >= 300:
        raise SystemExit(f"{method} failed with HTTP {response.status_code}: {urlsplit(url).path}")
    return response


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    args = parser.parse_args()
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("GH_TOKEN is required")
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "AcousticVectorReleaseVerifier/1",
        }
    )
    release_url = f"https://api.github.com/repos/{args.repository}/releases/tags/{args.tag}"
    release = request(session, "GET", release_url).json()
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    preview = "-preview." in version
    if release.get("draft"):
        raise SystemExit("Release is still a draft")
    if bool(release.get("prerelease")) != preview:
        raise SystemExit("Release prerelease flag does not match VERSION channel")
    assets = {item["name"]: item for item in release.get("assets", [])}
    if "update.json" not in assets:
        raise SystemExit("Release asset update.json is missing")
    manifest_response = request(session, "GET", assets["update.json"]["browser_download_url"])
    raw = decode_manifest(manifest_response.content)
    public_key = (ROOT / "updater" / "public_key.pem").read_bytes()
    verify_manifest_signature(raw, public_key)
    manifest = parse_manifest(raw)
    if manifest.version != version:
        raise SystemExit(f"Manifest version {manifest.version!r} != VERSION {version!r}")
    required_platforms = {"macos-arm64", "windows-x64"}
    if set(manifest.platforms) != required_platforms:
        raise SystemExit("Manifest does not contain exactly macos-arm64 and windows-x64")
    for platform_key, artifact in manifest.platforms.items():
        asset_name = Path(unquote(urlsplit(artifact.url).path)).name
        asset_name_is_unsigned = "-unsigned" in Path(asset_name).stem
        if asset_name_is_unsigned != (artifact.system_signature == "unsigned"):
            raise SystemExit(f"Asset filename/signature label mismatch for {platform_key}")
        required_assets = {asset_name, f"{asset_name}.sha256", f"{asset_name}.sig"}
        missing_assets = sorted(required_assets - assets.keys())
        if missing_assets:
            raise SystemExit(
                f"Manifest {platform_key} assets are absent from Release: {', '.join(missing_assets)}"
            )
        head = request(session, "HEAD", artifact.url)
        content_length = head.headers.get("Content-Length")
        if content_length and int(content_length) != artifact.size:
            raise SystemExit(f"Remote size mismatch for {platform_key}")
        response = request(session, "GET", artifact.url, stream=True)
        digest = hashlib.sha256()
        downloaded = 0
        try:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    downloaded += len(chunk)
                    digest.update(chunk)
        finally:
            response.close()
        if downloaded != artifact.size:
            raise SystemExit(f"Downloaded size mismatch for {platform_key}")
        if digest.hexdigest().lower() != artifact.sha256.lower():
            raise SystemExit(f"Downloaded SHA-256 mismatch for {platform_key}")
        verify_artifact_signature(manifest.version, platform_key, artifact, public_key)
    if not preview and release.get("prerelease"):
        raise SystemExit("Stable Release must not be a prerelease")
    print(f"Verified release {args.tag}: update.json and both platform assets are accessible")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
