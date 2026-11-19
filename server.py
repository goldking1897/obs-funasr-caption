"""本地 HTTP 服务：字幕页面（SSE）与控制台 API。"""
from __future__ import annotations

import json
import logging
import mimetypes
import queue
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from app import App
from config import ConfigError
from events import dumps_event

LOG = logging.getLogger("http")
WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 1 << 20

STATIC_ROUTES = {
    "/overlay": WEB_DIR / "overlay.html",
    "/control": WEB_DIR / "control.html",
}


class CaptionHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    app: App

    def handle_error(self, request: Any, client_address: Any) -> None:
        """浏览器关闭 SSE/刷新页面时断开连接是正常现象，不打印堆栈。"""
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, TimeoutError)):
            LOG.debug("客户端断开：%s (%s)", client_address, exc)
            return
        LOG.exception("处理请求失败：%s", client_address)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FunASRCaption/1.0"
    sys_version = ""

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # 静音默认的逐请求 stderr 日志，只保留错误
    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # ---------- 工具 ----------

    def _send_bytes(self, status: int, body: bytes, content_type: str, *, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if not cache else "max-age=3600")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

    def _send_json(self, obj: Any, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self._send_bytes(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str) -> None:
        self._send_json({"ok": False, "error": message}, status=status)

    def _read_json(self) -> dict[str, Any]:
        length_raw = self.headers.get("Content-Length")
        if not length_raw:
            raise ValueError("缺少 Content-Length")
        try:
            length = int(length_raw)
        except ValueError as exc:
            raise ValueError("Content-Length 无效") from exc
        if length < 0 or length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求体不是合法 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        try:
            parts = urlsplit(origin)
        except ValueError:
            return False
        return parts.hostname in ("127.0.0.1", "localhost", "::1")

    def _content_type_ok(self) -> bool:
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        return ctype == "application/json"

    # ---------- 路由 ----------

    def do_GET(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        try:
            if path == "/":
                self.send_response(HTTPStatus.FOUND)
                self.send_header("Location", "/control")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == "/api/config":
                self._send_json({"ok": True, "config": self.app.snapshot()["config"], "style": self.app.snapshot()["style"]})
                return
            if path == "/api/state":
                self._send_json({"ok": True, **self.app.snapshot()})
                return
            if path == "/api/devices":
                self._send_json({"ok": True, **self.app.devices()})
                return
            if path == "/api/events":
                self._serve_events()
                return
            if path == "/favicon.ico":
                self._send_bytes(204, b"", "image/x-icon")
                return
            target = STATIC_ROUTES.get(path)
            if target is None and path.startswith("/assets/"):
                candidate = (WEB_DIR / path.lstrip("/")).resolve()
                if candidate.is_file() and WEB_DIR.resolve() in candidate.parents:
                    target = candidate
            if target is not None and target.is_file():
                ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                if target.suffix == ".html":
                    ctype = "text/html; charset=utf-8"
                self._send_bytes(200, target.read_bytes(), ctype)
                return
            self._error(404, "页面不存在")
        except Exception as exc:  # 避免单个请求异常拖垮服务
            LOG.exception("GET %s 失败", self.path)
            try:
                self._error(500, f"服务器错误：{exc}")
            except Exception:
                pass

    def do_POST(self) -> None:
        path = urlsplit(self.path).path.rstrip("/") or "/"
        if not self._origin_ok():
            self._error(403, "只接受来自本机的请求")
            return
        if path == "/api/config":
            if not self._content_type_ok():
                self._error(415, "Content-Type 必须是 application/json")
                return
            try:
                patch = self._read_json()
                result = self.app.update_config(patch)
            except ConfigError as exc:
                self._error(400, str(exc))
                return
            except ValueError as exc:
                self._error(400, str(exc))
                return
            except Exception as exc:
                LOG.exception("保存配置失败")
                self._error(500, f"保存配置失败：{exc}")
                return
            self._send_json(result)
            return
        if path in ("/api/restart", "/api/clear", "/api/devices/refresh"):
            # 这三个接口允许空 body
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= MAX_BODY:
                    self.rfile.read(length)
            except (ValueError, OSError):
                pass
            try:
                if path == "/api/restart":
                    self.app.restart(clear=False)
                    self._send_json({"ok": True})
                elif path == "/api/clear":
                    self.app.clear()
                    self._send_json({"ok": True})
                else:
                    self._send_json({"ok": True, **self.app.devices()})
            except Exception as exc:
                LOG.exception("POST %s 失败", path)
                self._error(500, f"操作失败：{exc}")
            return
        self._error(404, "接口不存在")

    def do_HEAD(self) -> None:
        self.do_GET()

    # ---------- SSE ----------

    def _chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii") + data + b"\r\n")
        self.wfile.flush()

    def _serve_events(self) -> None:
        q = self.app.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self._chunk(dumps_event(self.app.snapshot()).encode("utf-8"))
            while True:
                try:
                    event = q.get(timeout=15.0)
                except queue.Empty:
                    self._chunk(b": keepalive\n\n")
                    continue
                self._chunk(dumps_event(event).encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(q)
            self.close_connection = True


def serve(app: App, host: str, port: int) -> CaptionHTTPServer:
    server = CaptionHTTPServer((host, port), Handler)
    server.app = app
    thread = threading.Thread(target=server.serve_forever, name="http-server", daemon=True)
    thread.start()
    LOG.info("控制台：http://%s:%s/control", host, port)
    LOG.info("OBS 浏览器源：http://%s:%s/overlay", host, port)
    return server
