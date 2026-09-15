#!/usr/bin/env python3
"""Select signed or explicitly unsigned release mode without exposing secrets."""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


REQUIRED_SECRETS = {
    "macos": (
        "MACOS_CERTIFICATE_P12_BASE64",
        "MACOS_CERTIFICATE_PASSWORD",
        "APPLE_SIGNING_IDENTITY",
        "APPLE_ID",
        "APPLE_APP_SPECIFIC_PASSWORD",
        "APPLE_TEAM_ID",
    ),
    "windows": (
        "WINDOWS_CERTIFICATE_PFX_BASE64",
        "WINDOWS_CERTIFICATE_PASSWORD",
    ),
}
SIGNATURE_KINDS = {
    "macos": "apple-developer-id",
    "windows": "windows-authenticode",
}


class PartialSigningConfiguration(ValueError):
    pass


@dataclass(frozen=True)
class SigningMode:
    enabled: bool
    system_signature: str
    filename_suffix: str


def detect_signing_mode(platform_name: str, environment: Mapping[str, str]) -> SigningMode:
    required = REQUIRED_SECRETS[platform_name]
    present = tuple(name for name in required if environment.get(name, "").strip())
    if not present:
        return SigningMode(False, "unsigned", "-unsigned")
    if len(present) != len(required):
        missing = tuple(name for name in required if name not in present)
        raise PartialSigningConfiguration(
            "System signing secrets are only partially configured; missing: "
            + ", ".join(missing)
        )
    return SigningMode(True, SIGNATURE_KINDS[platform_name], "")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, choices=tuple(REQUIRED_SECRETS))
    parser.add_argument("--github-output", type=Path)
    args = parser.parse_args()
    try:
        mode = detect_signing_mode(args.platform, os.environ)
    except PartialSigningConfiguration as exc:
        raise SystemExit(str(exc)) from exc
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(
                f"enabled={'true' if mode.enabled else 'false'}\n"
                f"system_signature={mode.system_signature}\n"
                f"filename_suffix={mode.filename_suffix}\n"
            )
    label = mode.system_signature if mode.enabled else "unsigned test package"
    print(f"{args.platform}: {label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
