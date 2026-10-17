"""Offline test boundary: temporary state and no connections to real services."""
from __future__ import annotations

import os
from pathlib import Path
import socket
import tempfile

import pytest


# This runs before product modules are collected, including their global stores.
_SANDBOX = Path(tempfile.mkdtemp(prefix="xunjia-pytest-"))
_OWNED_TEST_ENDPOINTS = set()
for _name in tuple(os.environ):
    if _name.startswith(("XUNJIA_", "OSS_", "OSSBOX_", "QTRADE_", "QB_", "OPENAI_", "B_")):
        os.environ.pop(_name)
os.environ.update({
    "IMLINK_PATH": str(_SANDBOX / "im_link.json"),
    "XUNJIA_COMMANDS_PATH": str(_SANDBOX / "oss_commands.json"),
    "QB_CONFIG_DIR": str(_SANDBOX / "qb"),
    "QTRADE_AGENT_DB": str(_SANDBOX / "absent-agent.db"),
    "B_STATE_DIR": str(_SANDBOX / "relay-state"),
    "XUNJIA_QB_AUTOSTART": "0",
})


@pytest.fixture(autouse=True)
def offline_network(monkeypatch):
    """Only sockets bound by this test may receive loopback TCP connections."""
    bound = set()
    original_bind = socket.socket.bind
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo

    def bind(sock, address):
        result = original_bind(sock, address)
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host, port, *_ = sock.getsockname()
            if host in ("127.0.0.1", "::1"):
                bound.add((host, port))
        return result

    def check(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host, port, *_ = address
            if (host, port) not in bound and (host, port) not in _OWNED_TEST_ENDPOINTS:
                raise AssertionError("Real network connections are forbidden in tests")

    def connect(sock, address):
        check(sock, address)
        return original_connect(sock, address)

    def connect_ex(sock, address):
        check(sock, address)
        return original_connect_ex(sock, address)

    def getaddrinfo(host, *args, **kwargs):
        if host not in (None, "localhost", "127.0.0.1", "::1"):
            raise AssertionError("External DNS is forbidden in tests")
        return original_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "bind", bind)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture
def allow_owned_loopback_endpoint():
    """QA fixtures may register only a loopback endpoint they just created."""
    registered = set()

    def allow(host, port):
        if host != "127.0.0.1" or type(port) is not int or not 1024 < port < 65536:
            raise AssertionError("Only an explicitly created loopback test service is allowed")
        registered.add((host, port))
        _OWNED_TEST_ENDPOINTS.add((host, port))

    yield allow
    _OWNED_TEST_ENDPOINTS.difference_update(registered)


def pytest_report_header():
    return f"Offline state root: {_SANDBOX}"
