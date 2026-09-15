from __future__ import annotations

import pytest

from scripts.detect_signing_mode import (
    PartialSigningConfiguration,
    REQUIRED_SECRETS,
    detect_signing_mode,
)


@pytest.mark.parametrize("platform_name", ("macos", "windows"))
def test_no_system_signing_secrets_selects_unsigned(platform_name: str) -> None:
    mode = detect_signing_mode(platform_name, {})
    assert not mode.enabled
    assert mode.system_signature == "unsigned"
    assert mode.filename_suffix == "-unsigned"


@pytest.mark.parametrize(
    ("platform_name", "signature_kind"),
    (("macos", "apple-developer-id"), ("windows", "windows-authenticode")),
)
def test_complete_system_signing_secrets_enable_signing(
    platform_name: str, signature_kind: str
) -> None:
    environment = {name: "configured" for name in REQUIRED_SECRETS[platform_name]}
    mode = detect_signing_mode(platform_name, environment)
    assert mode.enabled
    assert mode.system_signature == signature_kind
    assert mode.filename_suffix == ""


@pytest.mark.parametrize("platform_name", ("macos", "windows"))
def test_partial_system_signing_configuration_fails_closed(platform_name: str) -> None:
    first = REQUIRED_SECRETS[platform_name][0]
    with pytest.raises(PartialSigningConfiguration, match="partially configured"):
        detect_signing_mode(platform_name, {first: "configured"})
