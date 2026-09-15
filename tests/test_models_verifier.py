from __future__ import annotations

import base64
import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from conftest import make_signed_manifest
from scripts.make_update_manifest import (
    artifact_payload as release_artifact_payload,
    manifest_payload as release_manifest_payload,
)
from updater.errors import UpdateError
from updater.models import decode_manifest, parse_manifest
from updater.verifier import (
    artifact_signature_payload,
    load_public_key,
    manifest_signature_payload,
    verify_artifact_signature,
    verify_file_hash,
    verify_manifest_signature,
)


ROOT = Path(__file__).resolve().parents[1]


def test_manifest_and_artifact_signatures(signing_key, public_key_pem, tmp_path) -> None:
    payload = b"signed application payload"
    raw = make_signed_manifest(signing_key, "https://example.com/update.zip", payload)
    verify_manifest_signature(raw, public_key_pem)
    manifest = parse_manifest(raw)
    artifact = manifest.platforms["macos-arm64"]
    verify_artifact_signature(manifest.version, "macos-arm64", artifact, public_key_pem)
    package = tmp_path / "update.zip"
    package.write_bytes(payload)
    verify_file_hash(package, artifact.sha256)


def test_tampered_manifest_is_rejected(signing_key, public_key_pem) -> None:
    raw = make_signed_manifest(signing_key, "https://example.com/update.zip", b"payload")
    raw["notes"] = "tampered"
    with pytest.raises(UpdateError) as failure:
        verify_manifest_signature(raw, public_key_pem)
    assert failure.value.code == "signature"


def test_wrong_artifact_signature_is_rejected(signing_key, public_key_pem) -> None:
    signature = base64.b64encode(b"\0" * 64).decode("ascii")
    raw = make_signed_manifest(
        signing_key,
        "https://example.com/update.zip",
        b"payload",
        artifact_signature_override=signature,
    )
    verify_manifest_signature(raw, public_key_pem)
    manifest = parse_manifest(raw)
    with pytest.raises(UpdateError) as failure:
        verify_artifact_signature(
            manifest.version, "macos-arm64", manifest.platforms["macos-arm64"], public_key_pem
        )
    assert failure.value.code == "signature"


def test_hash_mismatch_is_rejected(tmp_path) -> None:
    package = tmp_path / "update.zip"
    package.write_bytes(b"wrong")
    with pytest.raises(UpdateError) as failure:
        verify_file_hash(package, hashlib.sha256(b"right").hexdigest())
    assert failure.value.code == "hash"


def test_duplicate_json_keys_are_rejected() -> None:
    with pytest.raises(UpdateError) as failure:
        decode_manifest(b'{"version":"1.0.0","version":"2.0.0"}')
    assert failure.value.code == "manifest_json"


def test_http_non_localhost_url_is_rejected(signing_key) -> None:
    raw = make_signed_manifest(signing_key, "http://example.com/update.zip", b"payload")
    with pytest.raises(UpdateError) as failure:
        parse_manifest(raw)
    assert failure.value.code == "invalid_url"


def test_release_and_client_canonical_signature_payloads_match(signing_key) -> None:
    raw = make_signed_manifest(signing_key, "https://example.com/update.zip", b"payload")
    item = raw["platforms"]["macos-arm64"]
    assert release_manifest_payload(raw) == manifest_signature_payload(raw)
    assert release_artifact_payload(raw["version"], "macos-arm64", item) == (
        artifact_signature_payload(raw["version"], "macos-arm64", item)
    )


def test_committed_release_public_key_is_valid_ed25519() -> None:
    raw = (ROOT / "updater" / "public_key.pem").read_bytes()
    # Loading happens in every signature verification call. This explicit
    # assertion makes an accidentally restored placeholder fail CI directly.
    assert isinstance(load_public_key(raw), Ed25519PublicKey)
