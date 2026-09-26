"""baostock_proxy 替换的 send_msg：断线、读超时、总时限都要能退出，正常响应的解包与官方一致。

官方 send_msg 在服务器断开时 recv 一直返回空字节、收包循环永不结束，
首次启动补基准时实测卡死过；这里用假 socket 覆盖几种网络异常。
"""
import socket
import types
import zlib

import pytest

baostock = pytest.importorskip("baostock")
import baostock.common.contants as cons  # noqa: E402

import baostock_proxy  # noqa: E402

END = b"<![CDATA[]]>\n"


def _header(message_type, body_length):
    return f"{cons.BAOSTOCK_CLIENT_VERSION}{cons.MESSAGE_SPLIT}{message_type}{cons.MESSAGE_SPLIT}{body_length:010d}"


class _FakeSocket:
    def __init__(self, chunks, timeout=None):
        self.chunks = list(chunks)
        self.sent = b""
        self.closed = False
        self._timeout = timeout

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value

    def sendall(self, data):
        self.sent += data

    def recv(self, _size):
        item = self.chunks.pop(0) if self.chunks else b""
        if isinstance(item, BaseException):
            raise item
        return item

    def close(self):
        self.closed = True


def _send(sock, message="req"):
    context = types.SimpleNamespace(default_socket=sock)
    send_msg = baostock_proxy._make_send_msg(cons, context)
    return send_msg(message), context


def test_plain_response_with_end_marker_split_across_chunks():
    body = "a,b,c"
    payload = (_header(cons.MESSAGE_TYPE_LOGIN_RESPONSE, len(body)) + body).encode() + END
    sock = _FakeSocket([payload[:10], payload[10:-5], payload[-5:]])
    result, context = _send(sock, "login")
    assert result == payload.decode()
    assert sock.sent == b"login\n"
    assert sock.gettimeout() == baostock_proxy.BAOSTOCK_READ_TIMEOUT
    assert context.default_socket is sock and not sock.closed


def test_compressed_kline_response_is_decompressed():
    rows = "2026-09-24,sh.000300,4439.1443"
    compressed = zlib.compress(rows.encode())
    head = _header(cons.MESSAGE_TYPE_GETKDATAPLUS_RESPONSE, len(compressed))
    sock = _FakeSocket([head.encode() + compressed + END])
    result, _ = _send(sock)
    assert result == head + rows


def test_server_disconnect_returns_none_instead_of_spinning():
    sock = _FakeSocket([b"partial", b""])  # 第二次 recv 返回空字节 = 对端已断开
    result, context = _send(sock)
    assert result is None
    assert sock.closed and context.default_socket is None


def test_read_timeout_returns_none_and_drops_connection():
    sock = _FakeSocket([b"partial", socket.timeout("timed out")], timeout=60)
    result, context = _send(sock)
    assert result is None
    assert sock.closed and context.default_socket is None


def test_request_deadline(monkeypatch):
    clock = iter([0.0, 1.0, 10_000.0])
    monkeypatch.setattr(baostock_proxy.time, "monotonic", lambda: next(clock))
    sock = _FakeSocket([b"part1", b"part2", b"part3"], timeout=60)
    result, context = _send(sock)
    assert result is None
    assert context.default_socket is None


def test_not_logged_in_or_dropped_socket():
    send_msg = baostock_proxy._make_send_msg(cons, types.SimpleNamespace())
    assert send_msg("x") is None
    send_msg = baostock_proxy._make_send_msg(cons, types.SimpleNamespace(default_socket=None))
    assert send_msg("x") is None


def test_enable_patches_send_msg_used_by_baostock_api(monkeypatch):
    import baostock.util.socketutil as socketutil

    monkeypatch.setattr(baostock_proxy, "_PATCHED", False)
    monkeypatch.setattr(socketutil, "send_msg", socketutil.send_msg)
    monkeypatch.setattr(socketutil.SocketUtil, "connect", socketutil.SocketUtil.connect)
    monkeypatch.setattr(socketutil, "get_default_socket", socketutil.get_default_socket)
    assert baostock_proxy.enable_baostock_proxy()
    assert socketutil.send_msg.__module__ == "baostock_proxy"


def test_recv_failure_marked_transient_for_retry():
    # 下载进程据此换新进程重新登录后重试；开源版把这组公共函数放在 worker_common
    try:
        from duckdb_storage.import_worker import _is_transient_data_source_error
    except ImportError:
        from duckdb_storage.worker_common import _is_transient_data_source_error

    assert _is_transient_data_source_error("网络接收错误。")
