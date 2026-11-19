"""演示模式：不连接云端、不打开音频设备，循环产出示例字幕。

用于先确认 OBS 浏览器源、字体和排版，再配置 API Key 实际使用。
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from events import CaptionState, EventBus

LOG = logging.getLogger("demo")

SENTENCES = [
    "大家好，这里是 OBS 实时字幕演示。",
    "现在使用的是本地演示数据，不会产生云端费用。",
    "配置好 DashScope 的 API Key 之后，就能识别系统声音。",
    "字幕会随着说话内容实时更新。",
    "中间结果用较浅的颜色显示，定稿后变成完整句子。",
    "你可以在控制台里调整字号、行数和颜色。",
    "按 Ctrl+C 可以停止这个本地服务。",
]


class DemoSource:
    def __init__(self, bus: EventBus, state: CaptionState, interval: float = 8.0) -> None:
        self._bus = bus
        self._state = state
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="demo-source", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _publish(self, sentence: dict[str, Any]) -> None:
        event = self._state.apply_sentence(sentence)
        if event is not None:
            self._bus.publish(event)

    def _run(self) -> None:
        self._state.new_session()
        self._bus.publish({"type": "status", "state": "demo", "message": "演示模式：本地示例字幕"})
        sentence_id = 0
        while not self._stop.is_set():
            for raw in SENTENCES:
                if self._stop.is_set():
                    return
                sentence_id += 1
                # 先逐字显示中间结果，模拟流式识别效果
                self._publish(
                    {
                        "sentence_id": sentence_id,
                        "sentence_begin": True,
                        "sentence_end": False,
                        "text": "",
                    }
                )
                chunk = ""
                for i, char in enumerate(raw):
                    chunk += char
                    if i % 4 == 3 or i == len(raw) - 1:
                        if self._stop.is_set():
                            return
                        self._publish(
                            {
                                "sentence_id": sentence_id,
                                "sentence_begin": False,
                                "sentence_end": False,
                                "text": chunk,
                            }
                        )
                        if self._stop.wait(0.09):
                            return
                self._publish(
                    {
                        "sentence_id": sentence_id,
                        "sentence_begin": False,
                        "sentence_end": True,
                        "text": raw,
                    }
                )
                if self._stop.wait(1.2):
                    return
            if self._stop.wait(self._interval):
                return
