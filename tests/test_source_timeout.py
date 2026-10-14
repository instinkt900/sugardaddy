"""Tests for the LibreLinkUp HTTP timeout (sugardaddy.source).

No test framework is required: run directly with

    python tests/test_source_timeout.py

pylibrelinkup makes its HTTP calls without a timeout, so a server that accepts the connection and never
answers used to block the ingest poller forever. These tests point the real client at a local socket that does
exactly that and check that the call gives up, and that ``latest()`` turns the failure into a ``SourceError``
(which the ingest loop handles by reconnecting next cycle). Nothing here touches the network.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sugardaddy import source  # noqa: E402
from sugardaddy.config import LibreLinkConfig  # noqa: E402


def _silent_server() -> tuple[str, socket.socket]:
    """A local server that accepts connections and never sends a byte."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            held.append(conn)

    threading.Thread(target=accept, daemon=True).start()
    host, port = srv.getsockname()
    return f"http://{host}:{port}", srv


def _client_against(url: str):
    from pylibrelinkup import APIUrl, PyLibreLinkUp

    client = PyLibreLinkUp(email="test@example.com", password="x", api_url=APIUrl.AU)
    client.api_url = url
    # Pretend to be logged in, so data calls go out instead of failing early.
    client._set_token("test-token")
    client._set_account_id_hash("test-account")
    return client


def test_install_is_idempotent() -> None:
    import pylibrelinkup.pylibrelinkup as client_module

    source._install_http_timeout()
    first = client_module.requests
    source._install_http_timeout()
    assert client_module.requests is first
    assert isinstance(first, source._TimeoutRequests)
    # Everything else still reaches the real module.
    assert first.HTTPError is __import__("requests").HTTPError


def test_hung_server_times_out() -> None:
    url, srv = _silent_server()
    saved = source.HTTP_TIMEOUT
    source.HTTP_TIMEOUT = (1.0, 1.0)
    try:
        source._install_http_timeout()
        client = _client_against(url)
        start = time.monotonic()
        try:
            client.authenticate()
        except Exception as exc:  # requests' ReadTimeout
            assert "timed out" in str(exc).lower(), exc
        else:
            raise AssertionError("authenticate() returned against a server that never answers")
        assert time.monotonic() - start < 5
    finally:
        source.HTTP_TIMEOUT = saved
        srv.close()


def test_latest_raises_source_error_on_hang() -> None:
    url, srv = _silent_server()
    saved = source.HTTP_TIMEOUT
    source.HTTP_TIMEOUT = (1.0, 1.0)
    try:
        source._install_http_timeout()
        src = source.LibreLinkUpSource(LibreLinkConfig(email="test@example.com", password="x"))
        src._client = _client_against(url)
        src._patient = "00000000-0000-0000-0000-000000000000"
        start = time.monotonic()
        try:
            src.latest()
        except source.SourceError as exc:
            assert "timed out" in str(exc).lower(), exc
        else:
            raise AssertionError("latest() returned against a server that never answers")
        assert time.monotonic() - start < 5
        # The client is dropped so the next poll reconnects.
        assert src._client is None
    finally:
        source.HTTP_TIMEOUT = saved
        srv.close()


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print("ok", name)
