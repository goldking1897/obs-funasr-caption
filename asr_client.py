"""阿里云 Qwen-Audio-ASR-Streaming WebSocket 客户端。

音频队列只保留很短的缓冲（约 2 秒），网络断开时丢弃旧音频，重连后继续，
避免把几十秒前的语音发到云端造成字幕滞后。
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

import websocket

from config import build_vocabulary
from events import CaptionState, EventBus

LOG = logging.getLogger("asr")

DEFAULT_WS_PATH = "/api-ws/v1/inference"
MAX_QUEUE_CHUNKS = 20


class ASRError(RuntimeError):
    pass


def build_ws_url(config: dict[str, Any]) -> str:
    explicit = (config.get("ws_url") or "").strip()
    if explicit:
        return explicit
    base = (config.get("base_url") or "https://dashscope.aliyuncs.com").strip().rstrip("/")
    parts = urlsplit(base)
    scheme = "wss" if parts.scheme in ("https", "wss") else "ws"
    path = parts.path.rstrip("/")
    if "/api-ws/" not in path:
        path = path + DEFAULT_WS_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def _sentence_of(message: dict[str, Any]) -> dict[str, Any] | None:
    payload = message.get("payload")
    if not isinstance(payload, dict):
        return None
    output = payload.get("output")
    if not isinstance(output, dict):
        return None
    sentence = output.get("sentence")
    return sentence if isinstance(sentence, dict) else None


class ASRClient:
    def __init__(
        self,
        get_config: Callable[[], dict[str, Any]],
        bus: EventBus,
        state: CaptionState,
        get_api_key: Callable[[], str],
    ) -> None:
        self._get_config = get_config
        self._bus = bus
        self._state = state
        self._get_api_key = get_api_key
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_alive = threading.Event()
        self._connected = threading.Event()
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=MAX_QUEUE_CHUNKS)
        self._status = "stopped"
        self._message = ""

    # ---------- 对外的生命周期 ----------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="asr-client", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._connected.clear()
        self._ws_alive.clear()
        self._drain_audio()
        if self._thread is not None:
            self._thread.join(timeout=4.0)
            self._thread = None
        self._status = "stopped"
        self._bus.publish({"type": "status", "state": "stopped", "message": "已停止"})

    def feed(self, chunk: bytes) -> None:
        """音频线程调用；未连接时直接丢弃，避免积压。"""
        if not self._connected.is_set():
            return
        try:
            self._queue.put_nowait(chunk)
        except queue.Full:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._queue.put_nowait(chunk)
            except queue.Full:
                pass

    @property
    def status(self) -> str:
        return self._status

    @property
    def message(self) -> str:
        return self._message

    def _drain_audio(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                return

    def _emit_status(self, status: str, message: str = "", **extra: Any) -> None:
        self._status = status
        self._message = message
        event = {"type": "status", "state": status, "message": message, **extra}
        LOG.info("状态 %s：%s", status, message)
        self._bus.publish(event)

    def _emit_error(self, message: str) -> None:
        LOG.error("ASR 错误：%s", message)
        self._bus.publish({"type": "error", "message": message})

    # ---------- 主循环 ----------

    def _run(self) -> None:
        delay = 1.0
        while not self._stop.is_set():
            config = self._get_config()
            if config.get("demo"):
                self._emit_status("demo", "演示模式（不连接云端）")
                return
            key = self._get_api_key()
            if not key:
                self._emit_error("还没有填写 DashScope API Key。请在控制台填写，或设置环境变量 DASHSCOPE_API_KEY。")
                self._emit_status("error", "缺少 API Key")
                return
            try:
                self._emit_status("connecting", "正在连接阿里云…")
                self._session(config, key)
                delay = 1.0
            except ASRError as exc:
                if self._stop.is_set():
                    break
                self._emit_error(str(exc))
                self._emit_status("reconnecting", f"{delay:.0f} 秒后重连")
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, 10.0)
            except Exception as exc:  # 网络库可能抛出各种异常
                LOG.exception("ASR 会话异常")
                if self._stop.is_set():
                    break
                self._emit_error(f"连接异常：{exc}")
                self._emit_status("reconnecting", f"{delay:.0f} 秒后重连")
                if self._stop.wait(delay):
                    break
                delay = min(delay * 2, 10.0)
            finally:
                self._connected.clear()
                self._ws_alive.clear()
                self._drain_audio()
        self._emit_status("stopped", "已停止")

    def _session(self, config: dict[str, Any], key: str) -> None:
        ws_url = build_ws_url(config)
        LOG.info("连接 %s", ws_url)
        ws = websocket.create_connection(
            ws_url,
            header=[f"Authorization: Bearer {key}"],
            timeout=10,
            enable_multithread=True,
        )
        sender: threading.Thread | None = None
        try:
            ws.settimeout(1.0)
            task_id = uuid.uuid4().hex
            self._send_run_task(ws, task_id, config)
            self._wait_task_started(ws)
            self._ws_alive.set()

            sender = threading.Thread(target=self._send_loop, args=(ws,), name="asr-sender", daemon=True)
            sender.start()
            self._receive_loop(ws, config, task_id)
        finally:
            self._connected.clear()
            self._ws_alive.clear()
            if sender is not None:
                sender.join(timeout=2.0)
            try:
                ws.close()
            except Exception:
                pass

    def _task_header(self, action: str, task_id: str) -> dict[str, Any]:
        return {"action": action, "task_id": task_id, "streaming": "duplex"}

    def _send_run_task(self, ws: websocket.WebSocket, task_id: str, config: dict[str, Any]) -> None:
        language = (config.get("language") or "").strip()
        parameters: dict[str, Any] = {
            "format": "pcm",
            "sample_rate": 16000,
            "heartbeat": bool(config.get("heartbeat", True)),
            "max_sentence_silence": int(config.get("max_sentence_silence") or 1000),
        }
        if language:
            parameters["language_hints"] = [language]
        model = config.get("model") or "qwen-audio-3.0-asr-flash-streaming"
        if "qwen-audio" in model.lower():
            vocabulary = build_vocabulary(config.get("hotwords"))
            if vocabulary:
                parameters["vocabulary"] = vocabulary
        message = {
            "header": self._task_header("run-task", task_id),
            "payload": {
                "task_group": "audio",
                "task": "asr",
                "function": "recognition",
                "model": model,
                "parameters": parameters,
                "input": {},
            },
        }
        ws.send(json.dumps(message, ensure_ascii=False))
        LOG.info("已发送 run-task (%s)", task_id[:8])

    def _wait_task_started(self, ws: websocket.WebSocket) -> None:
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self._stop.is_set():
                raise ASRError("已取消")
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ASRError("服务器关闭了连接")
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            event = (message.get("header") or {}).get("event")
            if event == "task-started":
                self._connected.set()
                self._state.new_session()
                self._emit_status("listening", "已连接，正在识别")
                return
            if event == "task-failed":
                raise ASRError(self._failure_text(message))
            LOG.debug("启动阶段收到事件：%s", event)
        raise ASRError("等待 task-started 超时，请检查网络、API Key 与模型权限")

    def _failure_text(self, message: dict[str, Any]) -> str:
        header = message.get("header") or {}
        code = header.get("error_code") or header.get("code") or ""
        detail = header.get("error_message") or header.get("message") or ""
        payload = message.get("payload") or {}
        if isinstance(payload, dict):
            detail = detail or payload.get("message") or payload.get("error_message") or ""
        text = "；".join(part for part in (str(code), str(detail)) if part)
        hints = {
            "InvalidApiKey": "API Key 无效或已删除，请到百炼控制台重新创建。",
            "AccessDenied": "该 API Key 没有语音识别权限，请确认已开通 Qwen-Audio-ASR / Fun-ASR 模型服务。",
            "Throttling": "请求过于频繁或并发超限，请稍后重试。",
            "ModelNotFound": "模型名不对，请检查 model 配置。",
        }
        hint = hints.get(str(code))
        return f"{text or '云端返回失败'}" + (f"（{hint}）" if hint else "")

    def _send_loop(self, ws: websocket.WebSocket) -> None:
        while not self._stop.is_set() and self._ws_alive.is_set():
            try:
                chunk = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if self._stop.is_set() or not self._ws_alive.is_set():
                return
            if not self._connected.is_set():
                # 正在轮换任务，旧音频已过期
                continue
            try:
                ws.send_binary(chunk)
            except Exception as exc:
                if not self._stop.is_set():
                    LOG.warning("发送音频失败：%s", exc)
                    self._connected.clear()
                    self._ws_alive.clear()
                return

    def _receive_loop(self, ws: websocket.WebSocket, config: dict[str, Any], task_id: str) -> None:
        task_started_at = time.monotonic()
        max_seconds = int(config.get("max_task_seconds") or 0)
        rotate_requested = False

        while not self._stop.is_set():
            if max_seconds and not rotate_requested and time.monotonic() - task_started_at >= max_seconds:
                rotate_requested = True
                LOG.info("达到单任务时长上限，轮换任务")
                self._connected.clear()
                self._drain_audio()
                try:
                    ws.send(json.dumps({"header": self._task_header("finish-task", task_id), "payload": {"input": {}}}))
                except Exception as exc:
                    raise ASRError(f"轮换任务时发送失败：{exc}") from exc
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                continue
            if not raw:
                raise ASRError("连接被服务器关闭")
            if isinstance(raw, bytes):
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                LOG.debug("忽略非 JSON 消息：%r", raw[:200])
                continue

            header = message.get("header") or {}
            event = header.get("event")
            if event == "result-generated":
                sentence = _sentence_of(message)
                if sentence is not None:
                    caption = self._state.apply_sentence(sentence)
                    if caption is not None:
                        self._bus.publish(caption)
            elif event == "task-finished":
                if rotate_requested:
                    rotate_requested = False
                    task_id = uuid.uuid4().hex
                    task_started_at = time.monotonic()
                    self._send_run_task(ws, task_id, config)
                    self._wait_task_started(ws)
                    LOG.info("任务轮换完成")
                else:
                    raise ASRError("云端结束了识别任务，正在重连")
            elif event == "task-failed":
                raise ASRError(self._failure_text(message))
            else:
                LOG.debug("收到事件：%s", event)
