"""Typed, user-facing updater failures."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit


def safe_url(url: str) -> str:
    """Remove credentials, query strings and fragments before logging/displaying a URL."""
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname or ""
        if ":" in hostname and not hostname.startswith("["):
            hostname = f"[{hostname}]"
        netloc = hostname
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
    except (TypeError, ValueError):
        return "<invalid-url>"


@dataclass(eq=False)
class UpdateError(Exception):
    code: str
    message: str
    detail: str = ""

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def user_text(self) -> str:
        return f"{self.message}\n\n{self.detail}" if self.detail else self.message


class UpdateCancelled(UpdateError):
    def __init__(self) -> None:
        super().__init__("cancelled", "更新下载已取消。")
