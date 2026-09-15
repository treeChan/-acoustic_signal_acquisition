from __future__ import annotations

from scripts.validate_release import api_session, public_download_session


def test_release_api_token_is_not_added_to_public_downloads() -> None:
    token = "test-token-that-must-not-leak"
    github_api = api_session(token)
    public_downloads = public_download_session()

    assert github_api.headers["Authorization"] == f"Bearer {token}"
    assert "Authorization" not in public_downloads.headers
