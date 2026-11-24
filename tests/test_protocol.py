"""用本地假 WebSocket 上游验证 Qwen-Audio / Fun-ASR 协议收发。

运行： .venv\\Scripts\\python.exe tests\\test_protocol.py
不依赖云端、不需要 API Key。
"""
from __future__ import annotations

import asyncio
import json
import queue
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import websockets  # noqa: E402

from asr_client import ASRClient, build_ws_url  # noqa: E402
from events import CaptionState, EventBus  # noqa: E402

TASK_ID = uuid.uuid4().hex
RUN_TASKS: list[dict] = []
AUDIO_BYTES = [0]
FINISHED = threading.Event()


def _sentence(sentence_id: int, text: str, end: bool) -> str:
    return json.dumps(
        {
            "header": {
                "event": "result-generated",
                "task_id": TASK_ID,
                "attributes": {},
            },
            "payload": {
                "output": {
                    "sentence": {
                        "sentence_id": sentence_id,
                        "sentence_begin": False,
                        "sentence_end": end,
                        "text": text,
                        "begin_time": 0,
                        "end_time": 1000,
                        "words": [],
                    }
                },
                "usage": None,
            },
        },
        ensure_ascii=False,
    )


async def handler(ws) -> None:
    request = getattr(ws, "request", None)
    headers = getattr(request, "headers", {})
    assert headers.get("Authorization", "").startswith("Bearer "), "缺少鉴权头"
    path = getattr(request, "path", "")
    assert path == "/api-ws/v1/inference", f"路径不对：{path}"

    first = await asyncio.wait_for(ws.recv(), timeout=5)
    message = json.loads(first)
    header = message["header"]
    payload = message["payload"]
    assert header["action"] == "run-task"
    assert header["streaming"] == "duplex"
    assert len(header["task_id"]) == 32
    assert payload["model"] == "qwen-audio-3.0-asr-flash-streaming"
    assert payload["parameters"]["sample_rate"] == 16000
    assert payload["parameters"]["format"] == "pcm"
    assert payload["parameters"]["vocabulary"] == {"阿里云": 50, "FunASR": 5}
    RUN_TASKS.append(message)

    await ws.send(
        json.dumps(
            {
                "header": {"event": "task-started", "task_id": header["task_id"], "attributes": {}},
                "payload": {},
            }
        )
    )

    chunks = 0
    try:
        while True:
            data = await asyncio.wait_for(ws.recv(), timeout=8)
            if isinstance(data, (bytes, bytearray)):
                chunks += 1
                AUDIO_BYTES[0] += len(data)
                if chunks == 2:
                    await ws.send(_sentence(1, "你好", False))
                if chunks == 4:
                    await ws.send(_sentence(1, "你好，世界。", True))
                if chunks == 6:
                    await ws.send(_sentence(2, "第二句", False))
                if chunks >= 8:
                    await ws.send(_sentence(2, "第二句也完成了。", True))
                continue
            message = json.loads(data)
            if message["header"]["action"] == "finish-task":
                await ws.send(
                    json.dumps(
                        {
                            "header": {"event": "task-finished", "task_id": header["task_id"], "attributes": {}},
                            "payload": {},
                        }
                    )
                )
                FINISHED.set()
                return
    except (asyncio.TimeoutError, websockets.exceptions.ConnectionClosed):
        return


class FakeServer:
    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self.port = 0
        self._ready = threading.Event()
        self._shutdown: asyncio.Event | None = None
        self._server = None
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)

        async def main() -> None:
            self._server = await websockets.serve(handler, "127.0.0.1", 0)
            self.port = self._server.sockets[0].getsockname()[1]
            self._shutdown = asyncio.Event()
            self._ready.set()
            await self._shutdown.wait()
            self._server.close()
            await self._server.wait_closed()

        try:
            self.loop.run_until_complete(main())
        finally:
            self.loop.close()

    def start(self) -> int:
        self.thread.start()
        assert self._ready.wait(5), "假服务器启动超时"
        return self.port

    def stop(self) -> None:
        if self._shutdown is not None:
            self.loop.call_soon_threadsafe(self._shutdown.set)
        self.thread.join(timeout=3.0)


def wait_for_event(events: queue.Queue, kind: str, timeout: float = 10.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event.get("type") == kind:
            return event
    raise AssertionError(f"等待 {kind} 事件超时")


def main() -> int:
    server = FakeServer()
    port = server.start()
    config = {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1",
        "ws_url": f"ws://127.0.0.1:{port}/api-ws/v1/inference",
        "model": "qwen-audio-3.0-asr-flash-streaming",
        "hotwords": ["阿里云=50", "FunASR"],
        "language": "zh",
        "max_sentence_silence": 1000,
        "heartbeat": True,
        "max_task_seconds": 3600,
        "demo": False,
    }
    bus = EventBus()
    state = CaptionState()
    events = bus.subscribe()
    client = ASRClient(lambda: config, bus, state, lambda: "test-key")
    try:
        assert build_ws_url({"base_url": "https://dashscope.aliyuncs.com"}).endswith("/api-ws/v1/inference")
        assert build_ws_url({"base_url": "https://x.cn-beijing.maas.aliyuncs.com"}).startswith("wss://x.cn")

        client.start()
        listening = wait_for_event(events, "status")
        while listening.get("state") != "listening":
            listening = wait_for_event(events, "status")

        # 每块 3200 字节 = 100 ms 16k/16bit 单声道
        for _ in range(12):
            client.feed(b"\x00\x00" * 1600)
            time.sleep(0.02)

        caption = wait_for_event(events, "caption", timeout=5)
        assert caption["partial"] == "你好", caption
        while "你好，世界。" not in caption["finals"]:
            caption = wait_for_event(events, "caption", timeout=5)
        assert caption["finals"][-1] == "你好，世界。"
        print("收到定稿字幕：", caption["finals"])

        while "第二句也完成了。" not in caption["finals"]:
            caption = wait_for_event(events, "caption", timeout=5)
        assert AUDIO_BYTES[0] >= 12 * 3200, AUDIO_BYTES
        assert RUN_TASKS and RUN_TASKS[0]["payload"]["parameters"]["language_hints"] == ["zh"]
        print(f"假上游收到音频 {AUDIO_BYTES[0]} 字节，run-task 参数正确")
        return 0
    finally:
        client.stop()
        server.stop()
        time.sleep(0.2)


if __name__ == "__main__":
    raise SystemExit(main())
