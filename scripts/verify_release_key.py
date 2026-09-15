#!/usr/bin/env python3
"""Fail release builds unless the private secret matches the committed public key."""

from __future__ import annotations

import argparse
import base64
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def private_key(path: Path) -> Ed25519PrivateKey:
    data = path.read_bytes()
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except ValueError:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(data.strip(), validate=True))
    if not isinstance(key, Ed25519PrivateKey):
        raise SystemExit("Private key is not Ed25519")
    return key


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--private-key", required=True, type=Path)
    parser.add_argument("--public-key", required=True, type=Path)
    args = parser.parse_args()
    private = private_key(args.private_key)
    try:
        public = serialization.load_pem_public_key(args.public_key.read_bytes())
    except ValueError as exc:
        raise SystemExit("Committed public key is not configured or is invalid") from exc
    if not isinstance(public, Ed25519PublicKey):
        raise SystemExit("Public key is not Ed25519")
    expected = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    actual = public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    if expected != actual:
        raise SystemExit("GitHub secret private key does not match updater/public_key.pem")
    print("Release signing key matches the embedded public key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
