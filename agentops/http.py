"""有界 HTTP；不把携带凭据的 URL 或响应正文写入普通异常。"""
from __future__ import annotations

import base64
import http.client
import json
import queue
import socket
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from dataclasses import dataclass

from .common import OpsError


@dataclass
class Response:
    status: int
    headers: dict
    body: bytes

    def json(self):
        try:
            return json.loads(self.body)
        except (ValueError, UnicodeError):
            raise OpsError("NOT_JSON", "接口返回的不是有效 JSON。", http_status=self.status)


class Transport:
    def __init__(self, connect_timeout=10, read_timeout=60, wall_timeout=90,
                 allow_local_test=False, use_proxy=True, proxy_url=None):
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self.wall_timeout = wall_timeout
        self.allow_local_test = allow_local_test
        self.use_proxy = use_proxy
        if proxy_url is not None:
            try:
                parsed = urllib.parse.urlsplit(proxy_url)
                valid = (parsed.scheme == "http" and parsed.hostname and parsed.port
                         and parsed.path in ("", "/") and not parsed.query and not parsed.fragment)
            except (TypeError, ValueError):
                valid = False
            if not valid:
                raise OpsError("PROXY_FORMAT", "本机 HTTP CONNECT 代理需要完整主机与端口。")
        self.proxy_url = proxy_url

    def _connect(self, address, timeout=None, source_address=None, **kwargs):
        """DNS 也有等待上限；不让阻塞的解析线程阻止 CLI 退出。"""
        answers = queue.Queue()
        def resolve():
            try:
                answers.put(socket.getaddrinfo(address[0], address[1], 0, socket.SOCK_STREAM))
            except BaseException as e:
                answers.put(e)
        threading.Thread(target=resolve, daemon=True).start()
        deadline = time.monotonic() + min(timeout or self.connect_timeout, self.wall_timeout)
        try:
            rows = answers.get(timeout=max(0.01, deadline-time.monotonic()))
        except queue.Empty:
            raise TimeoutError("DNS deadline")
        if isinstance(rows, BaseException):
            raise rows
        last = None
        for af, kind, proto, _, sockaddr in rows:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TimeoutError("connect deadline")
            sock = socket.socket(af, kind, proto)
            try:
                sock.settimeout(remaining)
                if source_address:
                    sock.bind(source_address)
                sock.connect(sockaddr)
                return sock
            except OSError as e:
                last = e
                sock.close()
        raise last or OSError("no address")

    def request(self, method, url, headers=None, body=None, sink=None, max_body=2_000_000):
        parsed = urllib.parse.urlsplit(url)
        local = parsed.hostname in ("127.0.0.1", "::1", "localhost")
        if parsed.username or parsed.password or parsed.fragment:
            raise OpsError("URL_FORMAT", "请求地址不能含用户信息或片段。")
        if parsed.scheme != "https" and not (local and self.allow_local_test and parsed.scheme == "http"):
            raise OpsError("HTTPS_REQUIRED", "网络请求必须使用 HTTPS。")
        hdr = dict(headers or {})
        hdr.setdefault("User-Agent", "netdisk; AgentOpsSkills/0.1")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        proxy = None
        if self.proxy_url and not local:
            proxy = self.proxy_url
        elif self.use_proxy and not local and not urllib.request.proxy_bypass(parsed.hostname):
            proxy = urllib.request.getproxies().get(parsed.scheme)
        conn_type = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        if proxy:
            pp = urllib.parse.urlsplit(proxy)
            if pp.scheme != "http":
                raise OpsError("PROXY_SCHEME", "此入口只使用 HTTP CONNECT 代理；请使用已验证的原生入口或配置 HTTP 代理。")
            conn = conn_type(pp.hostname, pp.port or 80, timeout=self.connect_timeout)
            tunnel_headers = {}
            if pp.username:
                raw = urllib.parse.unquote(pp.username) + ":" + urllib.parse.unquote(pp.password or "")
                tunnel_headers["Proxy-Authorization"] = "Basic " + base64.b64encode(raw.encode()).decode()
            conn.set_tunnel(parsed.hostname, port, headers=tunnel_headers)
        else:
            conn = conn_type(parsed.hostname, port, timeout=self.connect_timeout)
        conn._create_connection = self._connect
        timed_out = threading.Event()
        active_socket = [None]

        def expire():
            timed_out.set()
            # HTTP/1.0 getresponse 会把连接从 conn 分离；响应仍持有原 socket。
            sock = active_socket[0] or conn.sock
            if sock is not None:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass

        timer = threading.Timer(self.wall_timeout, expire)
        timer.daemon = True
        timer.start()
        try:
            conn.connect()
            active_socket[0] = conn.sock
            if timed_out.is_set():
                raise TimeoutError()
            conn.sock.settimeout(self.read_timeout)
            path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
            conn.request(method, path, body=body, headers=hdr)
            response = conn.getresponse()
            response_headers = {k.lower(): v for k, v in response.getheaders()}
            if sink is not None and response.status in (200, 206):
                while True:
                    data = response.read(128 * 1024)
                    if not data:
                        break
                    sink(data)
                    if timed_out.is_set():
                        raise TimeoutError()
                data = b""
            else:
                data = response.read(max_body + 1)
                if len(data) > max_body:
                    raise OpsError("RESPONSE_TOO_LARGE", "控制接口响应超过预期大小。")
            if timed_out.is_set():
                raise TimeoutError()
            return Response(response.status, response_headers, data)
        except OpsError:
            raise
        except (OSError, TimeoutError, http.client.HTTPException) as exc:
            raise OpsError("REQUEST_TIMEOUT" if timed_out.is_set() or isinstance(exc, TimeoutError)
                           else "NETWORK_ERROR", "当前请求未取得有效完成回执；保留原任务。",
                           error_type=type(exc).__name__)
        finally:
            timer.cancel()
            conn.close()


def cloud_url(url):
    p = urllib.parse.urlsplit(url)
    host = (p.hostname or "").lower()
    if p.scheme != "https" or p.username or p.password or not any(
            host == domain or host.endswith("." + domain)
            for domain in ("baidu.com", "baidupcs.com")):
        raise OpsError("UNEXPECTED_CLOUD_HOST", "服务端返回了未验证的百度传输域名。")
    return url


def retry_after(headers, default=2):
    raw=headers.get("retry-after")
    if raw is None:
        return default
    try:
        return max(0,float(raw))
    except (TypeError, ValueError):
        try:
            return max(0,(parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
        except (TypeError,ValueError,OverflowError):
            return default

