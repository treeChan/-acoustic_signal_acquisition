"""HTTP client with explicit redirect and proxy behavior."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit
from urllib.request import getproxies, proxy_bypass

import requests

from .errors import UpdateError, safe_url
from .models import validate_download_url


REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


@dataclass(frozen=True)
class NetworkTimeouts:
    connect: float = 10.0
    read: float = 10.0
    no_progress: float = 30.0


class HttpClient:
    def __init__(
        self,
        *,
        timeouts: NetworkTimeouts | None = None,
        allow_localhost: bool = False,
        max_redirects: int = 5,
    ) -> None:
        self.timeouts = timeouts or NetworkTimeouts()
        self.allow_localhost = allow_localhost
        self.max_redirects = max_redirects
        self.session = requests.Session()
        self.session.headers.update(
            {"User-Agent": "AcousticVectorUpdater/1", "Accept-Encoding": "identity"}
        )
        self.session.trust_env = True

    @staticmethod
    def _proxies_for_url(url: str) -> dict[str, str]:
        """Read environment/native proxies per request and honor bypass rules."""
        hostname = urlsplit(url).hostname
        if hostname and proxy_bypass(hostname):
            return {}
        # urllib.getproxies() combines HTTP(S)/ALL_PROXY with macOS
        # SystemConfiguration or Windows Internet Settings.
        detected = getproxies()
        return {
            key: value
            for key, value in detected.items()
            if key in {"http", "https", "all"} and isinstance(value, str)
        }

    def _request_once(self, url: str, *, stream: bool) -> requests.Response:
        try:
            return self.session.get(
                url,
                stream=stream,
                allow_redirects=False,
                timeout=(self.timeouts.connect, self.timeouts.read),
                proxies=self._proxies_for_url(url),
            )
        except requests.exceptions.ProxyError as exc:
            raise UpdateError(
                "proxy",
                "代理服务器连接失败。",
                f"请检查系统代理或 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。目标：{safe_url(url)}",
            ) from exc
        except requests.exceptions.ConnectTimeout as exc:
            raise UpdateError("connect_timeout", "连接更新服务器超时。", safe_url(url)) from exc
        except requests.exceptions.ReadTimeout as exc:
            raise UpdateError("read_timeout", "更新服务器读取超时。", safe_url(url)) from exc
        except requests.exceptions.SSLError as exc:
            raise UpdateError("tls", "更新服务器 TLS 证书验证失败。", safe_url(url)) from exc
        except requests.exceptions.ConnectionError as exc:
            raise UpdateError("network", "网络连接失败。", safe_url(url)) from exc
        except requests.exceptions.RequestException as exc:
            raise UpdateError("network", "更新请求失败。", safe_url(url)) from exc

    def open(self, url: str, *, stream: bool = False) -> requests.Response:
        validate_download_url(url, allow_localhost=self.allow_localhost)
        current = url
        for redirect_count in range(self.max_redirects + 1):
            response = self._request_once(current, stream=stream)
            if response.status_code in REDIRECT_CODES:
                location = response.headers.get("Location")
                response.close()
                if not location:
                    raise UpdateError(
                        "redirect", "更新服务器返回重定向，但没有 Location 地址。", safe_url(current)
                    )
                if redirect_count >= self.max_redirects:
                    raise UpdateError("redirect", "更新下载重定向次数过多。", safe_url(current))
                current = urljoin(current, location)
                validate_download_url(current, allow_localhost=self.allow_localhost)
                continue
            if response.status_code < 200 or response.status_code >= 300:
                status = response.status_code
                response.close()
                raise UpdateError(
                    "http_status", f"更新服务器返回 HTTP {status}。", safe_url(current)
                )
            return response
        raise UpdateError("redirect", "更新下载重定向次数过多。", safe_url(current))

    def raise_stream_error(self, error: Exception, url: str, *, subject: str) -> None:
        """Translate errors raised after response headers into stable user-facing codes."""
        chain: list[BaseException] = []
        current: BaseException | None = error
        while current is not None and current not in chain:
            chain.append(current)
            current = current.__cause__ or current.__context__
        names = {type(item).__name__ for item in chain}
        if isinstance(error, requests.exceptions.ProxyError) or "ProxyError" in names:
            raise UpdateError(
                "proxy",
                "代理服务器在传输更新数据时失败。",
                f"请检查系统代理或 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。目标：{safe_url(url)}",
            ) from error
        if isinstance(error, requests.exceptions.ReadTimeout) or names & {
            "ReadTimeout",
            "ReadTimeoutError",
            "TimeoutError",
        }:
            raise UpdateError(
                "read_timeout", f"读取{subject}超时。", safe_url(url)
            ) from error
        if isinstance(error, requests.exceptions.SSLError) or "SSLError" in names:
            raise UpdateError("tls", "更新传输的 TLS 证书验证失败。", safe_url(url)) from error
        if isinstance(error, requests.exceptions.ChunkedEncodingError):
            raise UpdateError(
                "download_interrupted", f"{subject}传输未完整结束。", safe_url(url)
            ) from error
        if isinstance(error, requests.exceptions.ConnectionError):
            raise UpdateError(
                "download_interrupted", f"{subject}传输连接中断。", safe_url(url)
            ) from error
        raise UpdateError("network", f"读取{subject}时发生网络错误。", safe_url(url)) from error
