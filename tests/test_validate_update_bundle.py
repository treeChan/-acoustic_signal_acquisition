from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from cryptography.hazmat.primitives import serialization

from conftest import make_signed_manifest
from scripts.validate_update_bundle import validate_bundle


def test_local_bundle_validation_checks_both_artifacts_and_signatures(
    signing_key, tmp_path
) -> None:
    data = b"release-candidate"
    raw = make_signed_manifest(signing_key, "https://example.com/package-unsigned.bin", data)
    (tmp_path / "package-unsigned.bin").write_bytes(data)
    (tmp_path / "update.json").write_text(json.dumps(raw), encoding="utf-8")
    public_key = tmp_path / "public.pem"
    public_key.write_bytes(
        signing_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    validate_bundle(tmp_path, public_key)

    (tmp_path / Path(urlsplit(raw["platforms"]["macos-arm64"]["url"]).path).name).write_bytes(
        b"tampered"
    )
    with pytest.raises(ValueError, match="size mismatch"):
        validate_bundle(tmp_path, public_key)
