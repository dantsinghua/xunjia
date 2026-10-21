"""Prevent the mock acceptance from silently reaching external Python sockets."""
import ipaddress,socket
import pytest
@pytest.fixture(autouse=True)
def loopback_only_python_network(monkeypatch):
    connect=socket.socket.connect;connect_ex=socket.socket.connect_ex
    def check(address):
        if isinstance(address,tuple):
            host=address[0]
            if host=='localhost':return
            try:allowed=ipaddress.ip_address(host).is_loopback
            except ValueError:allowed=False
            if not allowed:raise AssertionError('MOCK_ACCEPTANCE_EXTERNAL_NETWORK_FORBIDDEN')
    def guarded(self,address):check(address);return connect(self,address)
    def guarded_ex(self,address):check(address);return connect_ex(self,address)
    monkeypatch.setattr(socket.socket,'connect',guarded)
    monkeypatch.setattr(socket.socket,'connect_ex',guarded_ex)