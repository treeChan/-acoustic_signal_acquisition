from __future__ import annotations

import base64
import json
import os
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator

import pytest

from conftest import make_signed_manifest
from updater.checker import check_for_update
from updater.downloader import download_and_verify
from updater.errors import UpdateError
from updater.http_client import HttpClient, NetworkTimeouts


class Scenario:
    def __init__(self) -> None:
        self.artifact = b"release-package" * 4096
        self.manifest = b"{}"
        self.proxy_hits = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, _format: str, *_args: object) -> None:
        return

    @property
    def scenario(self) -> Scenario:
        return self.server.scenario  # type: ignore[attr-defined]

    def do_GET(self) -> None:
        path = self.path
        if path.startswith("http://localhost:1/manifest"):
            self.scenario.proxy_hits += 1
            path = "/manifest"
        if path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/manifest")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/missing":
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if path == "/timeout":
            time.sleep(1.0)
            return
        if path == "/manifest":
            body = self.scenario.manifest
        elif path == "/artifact":
            body = self.scenario.artifact
        elif path == "/interrupt":
            body = self.scenario.artifact
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body[: len(body) // 2])
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            return
        elif path == "/slow-artifact":
            body = self.scenario.artifact
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            for offset in range(0, len(body), 1024):
                self.wfile.write(body[offset : offset + 1024])
                self.wfile.flush()
                time.sleep(0.01)
            return
        else:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", "application/json" if path == "/manifest" else "application/octet-stream")
        self.end_headers()
        self.wfile.write(body)


@contextmanager
def server() -> Iterator[tuple[Scenario, str]]:
    scenario = Scenario()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.scenario = scenario  # type: ignore[attr-defined]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield scenario, f"http://127.0.0.1:{httpd.server_port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def configure_manifest(scenario, base_url, signing_key, **kwargs):
    raw = make_signed_manifest(signing_key, f"{base_url}/artifact", scenario.artifact, **kwargs)
    scenario.manifest = json.dumps(raw).encode("utf-8")
    return raw


def local_client(read_timeout: float = 1.0) -> HttpClient:
    return HttpClient(
        allow_localhost=True,
        timeouts=NetworkTimeouts(connect=0.5, read=read_timeout, no_progress=0.5),
    )


def test_normal_update_and_no_update(signing_key, public_key_pem) -> None:
    with server() as (scenario, base_url):
        configure_manifest(scenario, base_url, signing_key)
        update = check_for_update(
            "0.1.0",
            public_key_pem,
            platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest",
            client=local_client(),
            allow_localhost=True,
        )
        assert update.update_available
        current = check_for_update(
            "0.2.0",
            public_key_pem,
            platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest",
            client=local_client(),
            allow_localhost=True,
        )
        assert not current.update_available


def test_redirect_and_404(signing_key, public_key_pem) -> None:
    with server() as (scenario, base_url):
        configure_manifest(scenario, base_url, signing_key)
        result = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/redirect", client=local_client(), allow_localhost=True,
        )
        assert result.latest_version == "0.2.0"
        with pytest.raises(UpdateError) as failure:
            check_for_update(
                "0.1.0", public_key_pem, platform_key="macos-arm64",
                manifest_url=f"{base_url}/missing", client=local_client(), allow_localhost=True,
            )
        assert failure.value.code == "http_status"


def test_timeout_is_distinct(signing_key, public_key_pem) -> None:
    with server() as (_scenario, base_url):
        with pytest.raises(UpdateError) as failure:
            check_for_update(
                "0.1.0", public_key_pem, platform_key="macos-arm64",
                manifest_url=f"{base_url}/timeout", client=local_client(0.1), allow_localhost=True,
            )
        assert failure.value.code == "read_timeout"


def test_platform_missing(signing_key, public_key_pem) -> None:
    with server() as (scenario, base_url):
        configure_manifest(scenario, base_url, signing_key, platforms=("windows-x64",))
        with pytest.raises(UpdateError) as failure:
            check_for_update(
                "0.1.0", public_key_pem, platform_key="macos-arm64",
                manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
            )
        assert failure.value.code == "platform_missing"


def test_hash_and_signature_errors_cleanup(signing_key, public_key_pem, tmp_path) -> None:
    with server() as (scenario, base_url):
        configure_manifest(scenario, base_url, signing_key, sha256_override="0" * 64)
        check = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
        )
        with pytest.raises(UpdateError) as failure:
            download_and_verify(check, public_key_pem, client=local_client(), temporary_root=tmp_path)
        assert failure.value.code == "hash"
        assert list(tmp_path.iterdir()) == []

        bad_signature = base64.b64encode(b"\0" * 64).decode("ascii")
        configure_manifest(scenario, base_url, signing_key, artifact_signature_override=bad_signature)
        check = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
        )
        with pytest.raises(UpdateError) as failure:
            download_and_verify(check, public_key_pem, client=local_client(), temporary_root=tmp_path)
        assert failure.value.code == "signature"
        assert list(tmp_path.iterdir()) == []


def test_successful_download_verifies_and_cleanup_is_explicit(
    signing_key, public_key_pem, tmp_path
) -> None:
    with server() as (scenario, base_url):
        configure_manifest(scenario, base_url, signing_key)
        check = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
        )
        result = download_and_verify(
            check, public_key_pem, client=local_client(), temporary_root=tmp_path
        )
        assert result.path.read_bytes() == scenario.artifact
        assert not result.path.name.endswith(".part")
        result.cleanup()
        assert list(tmp_path.iterdir()) == []

def test_cancel_and_interrupted_download_cleanup(signing_key, public_key_pem, tmp_path) -> None:
    with server() as (scenario, base_url):
        raw = make_signed_manifest(signing_key, f"{base_url}/slow-artifact", scenario.artifact)
        scenario.manifest = json.dumps(raw).encode("utf-8")
        check = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
        )
        cancel = threading.Event()
        with pytest.raises(UpdateError) as failure:
            download_and_verify(
                check, public_key_pem, client=local_client(), cancel_event=cancel,
                progress_callback=lambda _progress: cancel.set(), temporary_root=tmp_path,
            )
        assert failure.value.code == "cancelled"
        assert list(tmp_path.iterdir()) == []

        raw = make_signed_manifest(signing_key, f"{base_url}/interrupt", scenario.artifact)
        scenario.manifest = json.dumps(raw).encode("utf-8")
        check = check_for_update(
            "0.1.0", public_key_pem, platform_key="macos-arm64",
            manifest_url=f"{base_url}/manifest", client=local_client(), allow_localhost=True,
        )
        with pytest.raises(UpdateError) as failure:
            download_and_verify(check, public_key_pem, client=local_client(), temporary_root=tmp_path)
        assert failure.value.code == "download_interrupted"
        assert list(tmp_path.iterdir()) == []


def test_http_proxy_environment_is_used(signing_key, public_key_pem, monkeypatch) -> None:
    with server() as (scenario, proxy_url):
        configure_manifest(scenario, proxy_url, signing_key)
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("http_proxy", proxy_url)
        monkeypatch.setenv("NO_PROXY", "")
        monkeypatch.setenv("no_proxy", "")
        result = check_for_update(
            "0.1.0",
            public_key_pem,
            platform_key="macos-arm64",
            manifest_url="http://localhost:1/manifest",
            client=local_client(),
            allow_localhost=True,
        )
        assert result.update_available
        assert scenario.proxy_hits == 1
