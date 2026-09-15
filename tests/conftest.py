from __future__ import annotations

import base64
import hashlib
from datetime import datetime, timezone
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from updater.verifier import artifact_signature_payload, manifest_signature_payload


@pytest.fixture
def signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.generate()


@pytest.fixture
def public_key_pem(signing_key: Ed25519PrivateKey) -> bytes:
    return signing_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def make_signed_manifest(
    private_key: Ed25519PrivateKey,
    artifact_url: str,
    artifact_data: bytes,
    *,
    version: str = "0.2.0",
    platforms: tuple[str, ...] = ("macos-arm64", "windows-x64"),
    sha256_override: str | None = None,
    artifact_signature_override: str | None = None,
) -> dict[str, Any]:
    digest = sha256_override or hashlib.sha256(artifact_data).hexdigest()
    platform_items: dict[str, dict[str, Any]] = {}
    for platform_key in platforms:
        item: dict[str, Any] = {
            "url": artifact_url,
            "size": len(artifact_data),
            "sha256": digest,
            "signature": "",
        }
        item["signature"] = artifact_signature_override or base64.b64encode(
            private_key.sign(artifact_signature_payload(version, platform_key, item))
        ).decode("ascii")
        platform_items[platform_key] = item
    manifest: dict[str, Any] = {
        "version": version,
        "notes": "## Changes\n\nSafe update notes.",
        "published_at": datetime.now(timezone.utc).isoformat(),
        "platforms": platform_items,
        "manifest_signature": "",
    }
    manifest["manifest_signature"] = base64.b64encode(
        private_key.sign(manifest_signature_payload(manifest))
    ).decode("ascii")
    return manifest
