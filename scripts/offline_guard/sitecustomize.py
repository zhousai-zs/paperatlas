"""Test-only process-wide TCP denial, inherited by MCP child via PYTHONPATH."""
import socket
import threading

_original_connect = socket.socket.connect
_original_connect_ex = socket.socket.connect_ex
_original_socketpair = socket.socketpair
_local = threading.local()


def deny_connect(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6} and not getattr(_local, 'socketpair', False):
        raise RuntimeError('Offline acceptance check blocked a TCP connection')
    return _original_connect(self, address)


def deny_connect_ex(self, address):
    if self.family in {socket.AF_INET, socket.AF_INET6} and not getattr(_local, 'socketpair', False):
        raise RuntimeError('Offline acceptance check blocked a TCP connection')
    return _original_connect_ex(self, address)


socket.socket.connect = deny_connect
socket.socket.connect_ex = deny_connect_ex


def local_socketpair(*args, **kwargs):
    # Windows asyncio implements its internal wake-up pipe using a loopback pair.
    # Permit only this synchronous stdlib operation, not arbitrary loopback requests.
    _local.socketpair = True
    try:
        return _original_socketpair(*args, **kwargs)
    finally:
        _local.socketpair = False


socket.socketpair = local_socketpair
