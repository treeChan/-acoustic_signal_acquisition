"""SHA-256 and Ed25519 verification for manifests and release artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .errors import UpdateError
from .models import UpdateArtifact


MANIFEST_DOMAIN = "acoustic-vector-update-manifest-v1"
ARTIFACT_DOMAIN = "acoustic-vector-update-artifact-v1"


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def manifest_signature_payload(raw: dict[str, Any]) -> bytes:
    unsigned = dict(raw)
    unsigned.pop("manifest_signature", None)
    return MANIFEST_DOMAIN.encode("ascii") + b"\0" + canonical_json(unsigned)


def artifact_signature_payload(
    version: str, platform_key: str, artifact: UpdateArtifact | dict[str, Any]
) -> bytes:
    if isinstance(artifact, UpdateArtifact):
        descriptor = {
            "version": version,
            "platform": platform_key,
            "url": artifact.url,
            "size": artifact.size,
            "sha256": artifact.sha256.lower(),
        }
    else:
        descriptor = {
            "version": version,
            "platform": platform_key,
            "url": artifact["url"],
            "size": artifact["size"],
            "sha256": str(artifact["sha256"]).lower(),
        }
    return ARTIFACT_DOMAIN.encode("ascii") + b"\0" + canonical_json(descriptor)


def load_public_key(public_key_pem: bytes) -> Ed25519PublicKey:
    try:
        key = serialization.load_pem_public_key(public_key_pem)
    except (ValueError, TypeError) as exc:
        raise UpdateError(
            "public_key", "内置更新公钥无效，无法安全检查更新。", "请重新安装官方版本。"
        ) from exc
    if not isinstance(key, Ed25519PublicKey):
        raise UpdateError("public_key", "内置更新公钥不是 Ed25519 公钥。")
    return key


def _decode_signature(signature: str, field: str) -> bytes:
    try:
        value = base64.b64decode(signature, validate=True)
    except (ValueError, TypeError) as exc:
        raise UpdateError("signature", f"{field} 不是有效的 Base64 签名。") from exc
    if len(value) != 64:
        raise UpdateError("signature", f"{field} 长度不是有效的 Ed25519 签名长度。")
    return value


def verify_signature(
    public_key: Ed25519PublicKey, payload: bytes, signature: str, field: str
) -> None:
    try:
        public_key.verify(_decode_signature(signature, field), payload)
    except InvalidSignature as exc:
        raise UpdateError(
            "signature", f"{field} 验证失败。", "更新内容可能被篡改，已停止更新。"
        ) from exc


def verify_manifest_signature(raw: dict[str, Any], public_key_pem: bytes) -> None:
    signature = raw.get("manifest_signature")
    if not isinstance(signature, str):
        raise UpdateError("signature", "更新清单缺少 manifest_signature。")
    verify_signature(
        load_public_key(public_key_pem),
        manifest_signature_payload(raw),
        signature,
        "更新清单签名",
    )


def verify_artifact_signature(
    version: str,
    platform_key: str,
    artifact: UpdateArtifact,
    public_key_pem: bytes,
) -> None:
    verify_signature(
        load_public_key(public_key_pem),
        artifact_signature_payload(version, platform_key, artifact),
        artifact.signature,
        "安装包签名",
    )


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def verify_file_hash(path: Path, expected_sha256: str) -> None:
    actual = sha256_file(path)
    if actual.lower() != expected_sha256.lower():
        raise UpdateError(
            "hash",
            "安装包 SHA-256 校验失败。",
            f"期望 {expected_sha256.lower()}，实际 {actual.lower()}。文件已拒绝安装。",
        )
