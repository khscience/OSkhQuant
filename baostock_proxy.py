#!/usr/bin/env python3
# coding: utf-8
"""BaoStock 代理适配。

BaoStock 官方 Python 包直接使用原始 TCP socket 连接
`public-api.baostock.com:10030`，不会读取 requests/curl 代理设置。

在受限网络环境下，DNS 或直连 TCP 可能失败；本模块会在这种情况下
自动尝试通过本机 HTTP 代理建立 CONNECT 隧道，并对 baostock 的
socket 创建逻辑做轻量 monkey patch。

官方 send_msg 的收包循环没有超时，服务器或代理断开连接时 recv 一直返回
空字节，循环永远不结束（首次启动补基准时实测卡死）。这里一并替换成带读
超时、总时限并识别断开的版本；失败时返回 None，baostock 各接口按
“网络接收错误”返回，由调用方重试或提示。
"""

from __future__ import annotations

import os
import socket
import struct
import random
import time
import zlib
from typing import Iterable, Optional, Tuple
from urllib.parse import urlparse


_PATCHED = False


def _env_seconds(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if value > 0 else default


# 单次 recv 多久收不到任何字节算网络卡死；整次请求（含大区间 K 线）的总时限
BAOSTOCK_READ_TIMEOUT = _env_seconds("KHQUANT_BAOSTOCK_READ_TIMEOUT", 60.0)
BAOSTOCK_REQUEST_DEADLINE = _env_seconds("KHQUANT_BAOSTOCK_REQUEST_DEADLINE", 300.0)
_MESSAGE_END = b"<![CDATA[]]>\n"


def _resolve_google_dns(hostname: str) -> Optional[str]:
    """直接向 8.8.8.8 查询 A 记录；只作为系统 DNS 解析失败时的后备。"""
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(5)
        sock.connect(('8.8.8.8', 53))
        txn_id = random.randint(0, 65535)
        query = struct.pack('>HBBHHHH', txn_id, 1, 0, 1, 0, 0, 0)
        for part in hostname.encode('ascii').split(b'.'):
            query += struct.pack('B', len(part)) + part
        query += b'\x00'
        query += struct.pack('>HH', 1, 1)
        sock.send(query)
        data = sock.recv(512)
        sock.close()
        if len(data) < 12:
            return None
        an_count = struct.unpack('>H', data[6:8])[0]
        if an_count == 0:
            return None
        pos = 12
        while pos < len(data) and data[pos] != 0:
            pos += data[pos] + 1
        pos += 5
        for _ in range(an_count):
            if data[pos] & 0xC0 == 0xC0:
                pos += 2
            else:
                while pos < len(data) and data[pos] != 0:
                    pos += data[pos] + 1
                pos += 1
            rtype = struct.unpack('>H', data[pos:pos+2])[0]
            pos += 8
            rdlen = struct.unpack('>H', data[pos:pos+2])[0]
            pos += 2
            if rtype == 1:
                return '.'.join(str(b) for b in data[pos:pos+rdlen])
            pos += rdlen
        return None
    except Exception:
        return None


def _resolve_host(hostname: str) -> str:
    """优先用系统 DNS 解析；系统解析失败时再向 8.8.8.8 查询。

    国内网络访问 8.8.8.8 常常不稳定，把它放在前面会让每次建立连接都先等
    满超时；系统 DNS 解析不到 baostock 域名的受限网络，仍可靠后备查询连通。
    """
    try:
        return socket.gethostbyname(hostname)
    except OSError as system_error:
        ip = _resolve_google_dns(hostname)
        if ip:
            return ip
        raise system_error


def _iter_proxy_candidates() -> Iterable[str]:
    """按优先级返回可尝试的代理地址。"""
    seen = set()
    for key in ("BAOSTOCK_PROXY_URL", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
        value = (os.environ.get(key) or "").strip()
        if value and value not in seen:
            seen.add(value)
            yield value

    proxy_host = (os.environ.get("KHQUANT_PROXY_HOST") or "").strip()
    if proxy_host:
        # 兼容由宿主环境只提供代理主机、使用常见本地代理端口 7890 的场景。
        candidate = f"http://{proxy_host}:7890"
        if candidate not in seen:
            seen.add(candidate)
            yield candidate


def _parse_proxy_url(proxy_url: str) -> Optional[Tuple[str, str, int]]:
    """解析代理 URL，当前仅支持 HTTP 代理。"""
    raw = proxy_url.strip()
    if not raw:
        return None

    if "://" not in raw:
        raw = f"http://{raw}"

    parsed = urlparse(raw)
    scheme = (parsed.scheme or "http").lower()
    host = parsed.hostname
    port = parsed.port or 80

    if not host or scheme != "http":
        return None
    return scheme, host, port


def _recv_until_header_end(sock: socket.socket, max_bytes: int = 8192) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and len(data) < max_bytes:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    return data


def _connect_via_http_proxy(
    proxy_host: str,
    proxy_port: int,
    target_host: str,
    target_port: int,
    timeout: float = 10.0,
) -> socket.socket:
    sock = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    request = (
        f"CONNECT {target_host}:{target_port} HTTP/1.1\r\n"
        f"Host: {target_host}:{target_port}\r\n"
        "Proxy-Connection: Keep-Alive\r\n"
        "\r\n"
    )
    sock.sendall(request.encode("ascii"))
    response = _recv_until_header_end(sock)
    first_line = response.split(b"\r\n", 1)[0].decode("latin1", errors="replace")
    if " 200 " not in first_line:
        sock.close()
        raise OSError(f"代理 CONNECT 失败: {first_line or 'empty response'}")
    return sock


def _create_socket(target_host: str, target_port: int, timeout: float = 10.0) -> socket.socket:
    """先解析 IP（系统 DNS 优先），再尝试直连，失败后按顺序尝试代理。"""
    resolved_ip = _resolve_host(target_host)

    direct_error = None
    try:
        return socket.create_connection((resolved_ip, target_port), timeout=timeout)
    except Exception as exc:
        direct_error = exc

    last_proxy_error = None
    for proxy_url in _iter_proxy_candidates():
        parsed = _parse_proxy_url(proxy_url)
        if not parsed:
            continue
        _, proxy_host, proxy_port = parsed
        try:
            return _connect_via_http_proxy(proxy_host, proxy_port, target_host, target_port, timeout=timeout)
        except Exception as exc:
            last_proxy_error = exc
            continue

    if last_proxy_error is not None:
        raise last_proxy_error
    if direct_error is not None:
        raise direct_error
    raise OSError("无法创建 BaoStock 连接")


def enable_baostock_proxy() -> bool:
    """为 baostock 打补丁，使其支持自动走代理。"""
    global _PATCHED
    if _PATCHED:
        return True

    try:
        import baostock.common.contants as cons
        import baostock.common.context as context
        import baostock.util.socketutil as socketutil
    except Exception:
        return False

    def _patched_connect(self):
        my_socket = None
        try:
            my_socket = _create_socket(cons.BAOSTOCK_SERVER_IP, cons.BAOSTOCK_SERVER_PORT)
            my_socket.settimeout(BAOSTOCK_READ_TIMEOUT)
        except Exception:
            print("服务器连接失败，请稍后再试。")
        setattr(context, "default_socket", my_socket)

    def _patched_get_default_socket():
        try:
            sock = _create_socket(cons.BAOSTOCK_SERVER_IP, cons.BAOSTOCK_SERVER_PORT)
            sock.settimeout(BAOSTOCK_READ_TIMEOUT)
            return sock
        except Exception:
            print("服务器连接失败，请稍后再试。")
            return None

    socketutil.SocketUtil.connect = _patched_connect
    socketutil.get_default_socket = _patched_get_default_socket
    socketutil.send_msg = _make_send_msg(cons, context)
    _PATCHED = True
    return True


def _make_send_msg(cons, context):
    """与官方 send_msg 的收发和解包一致，只是不会无限等待。"""

    def _drop_socket(sock):
        # 半截响应还留在连接里，这条连接不能再用；清掉后接口返回“网络接收错误”，
        # 下载进程据此换新进程重新登录，界面里的补基准则提示失败。
        try:
            sock.close()
        except Exception:
            pass
        setattr(context, "default_socket", None)

    def send_msg(msg):
        if not hasattr(context, "default_socket"):
            print("you don't login.")
            return None
        sock = getattr(context, "default_socket")
        if sock is None:
            return None
        try:
            if sock.gettimeout() is None:
                sock.settimeout(BAOSTOCK_READ_TIMEOUT)
            deadline = time.monotonic() + BAOSTOCK_REQUEST_DEADLINE
            sock.sendall(bytes(msg + "\n", encoding="utf-8"))
            chunks = []
            tail = b""
            while True:
                if time.monotonic() > deadline:
                    print(f"BaoStock 请求超过 {BAOSTOCK_REQUEST_DEADLINE:.0f} 秒仍未完成，已断开本次连接。")
                    _drop_socket(sock)
                    return None
                recv = sock.recv(8192)
                if not recv:
                    print("BaoStock 服务器断开了连接，请稍后再试。")
                    _drop_socket(sock)
                    return None
                chunks.append(recv)
                tail = (tail + recv)[-len(_MESSAGE_END):]
                if tail == _MESSAGE_END:
                    break
            receive = b"".join(chunks)
            head_bytes = receive[0:cons.MESSAGE_HEADER_LENGTH]
            head_str = bytes.decode(head_bytes)
            head_arr = head_str.split(cons.MESSAGE_SPLIT)
            if head_arr[1] in cons.COMPRESSED_MESSAGE_TYPE_TUPLE:
                head_inner_length = int(head_arr[2])
                body = receive[cons.MESSAGE_HEADER_LENGTH:cons.MESSAGE_HEADER_LENGTH + head_inner_length]
                return head_str + bytes.decode(zlib.decompress(body))
            return bytes.decode(receive)
        except (socket.timeout, OSError) as exc:
            print(f"BaoStock 网络超时或中断（{BAOSTOCK_READ_TIMEOUT:.0f} 秒无数据）：{exc}")
            _drop_socket(sock)
            return None
        except Exception as ex:
            print(ex)
            print("接收数据异常，请稍后再试。")
            return None

    return send_msg
