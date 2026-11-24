"""端到端：WASAPI 回环采集 → 转码 → ASR WebSocket → 字幕状态。

用本机扬声器播放一段提示音，假上游按收到的音频块返回字幕。
运行： .venv\\Scripts\\python.exe tests\\test_integration.py
"""
from __future__ import annotations

import json
import math
import queue
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import App  # noqa: E402
from test_protocol import FakeServer  # noqa: E402


def make_tone(path: str, seconds: float = 3.0, rate: int = 48000) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            value = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<hh", value, value)
        wf.writeframes(frames)


def wait_caption(events: queue.Queue, predicate, timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            event = events.get(timeout=0.2)
        except queue.Empty:
            continue
        if event.get("type") == "caption":
            last = event
            if predicate(event):
                return event
    raise AssertionError(f"等待字幕超时，最后一条：{last}")


def main() -> int:
    import winsound

    server = FakeServer()
    port = server.start()
    config = {
        "api_key": "test-key",
        "base_url": "http://127.0.0.1",
        "ws_url": f"ws://127.0.0.1:{port}/api-ws/v1/inference",
        "model": "qwen-audio-3.0-asr-flash-streaming",
        "hotwords": ["阿里云=50", "FunASR"],
        "language": "zh",
        "audio_source": "loopback",
        "device_index": None,
        "max_sentence_silence": 1000,
        "heartbeat": True,
        "max_task_seconds": 3600,
        "demo": False,
    }
    config_path = Path(tempfile.mktemp(suffix=".json"))
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    tone_path = tempfile.mktemp(suffix=".wav")
    make_tone(tone_path)

    app = App(config_path)
    events = app.bus.subscribe()
    try:
        app.start()
        winsound.PlaySound(tone_path, winsound.SND_FILENAME | winsound.SND_ASYNC)
        caption = wait_caption(events, lambda e: "你好，世界。" in e.get("finals", []))
        print("收到第一句定稿：", caption["finals"], flush=True)
        caption = wait_caption(events, lambda e: "第二句也完成了。" in e.get("finals", []))
        print("收到第二句定稿：", caption["finals"], flush=True)
        state = app.snapshot()
        print("状态：", state["status"], "音频：", state["audio"], flush=True)
        assert state["status"] in ("listening", "connecting", "reconnecting")
        assert state["audio"].get("device")
        print("OK", flush=True)
        return 0
    finally:
        winsound.PlaySound(None, winsound.SND_PURGE)
        app.stop()
        server.stop()
        config_path.unlink(missing_ok=True)
        Path(tone_path).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
