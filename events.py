"""线程安全的事件总线与字幕状态。"""
from __future__ import annotations

import json
import queue
import threading
import time
from collections import deque
from typing import Any


class EventBus:
    """把服务端事件分发给所有 SSE 订阅者。"""

    def __init__(self, max_queue: int = 200) -> None:
        self._subscribers: set[queue.Queue[dict[str, Any]]] = set()
        self._lock = threading.Lock()
        self._max_queue = max_queue

    def subscribe(self) -> queue.Queue[dict[str, Any]]:
        q: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=self._max_queue)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue[dict[str, Any]]) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: dict[str, Any]) -> None:
        payload = {**event, "ts": event.get("ts", time.time())}
        with self._lock:
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(payload)
            except queue.Full:
                # 浏览器页面短暂卡顿时，丢最旧的事件，保留最新字幕。
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(payload)
                except queue.Full:
                    pass


class CaptionState:
    """维护“已定稿句子 + 当前句中间结果”。"""

    def __init__(self, history: int = 20) -> None:
        self._lock = threading.Lock()
        self._finals: deque[str] = deque(maxlen=history)
        self._partial = ""
        self._last_update = 0.0
        self._session = 0
        self._finalized: set[tuple[int, str]] = set()

    def new_session(self) -> None:
        with self._lock:
            self._session += 1
            self._partial = ""

    def reset(self) -> None:
        with self._lock:
            self._finals.clear()
            self._partial = ""
            self._last_update = 0.0
            self._finalized.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "finals": list(self._finals)[-8:],
                "partial": self._partial,
                "last_update": self._last_update,
            }

    def apply_sentence(self, sentence: dict[str, Any], *, emit: bool = True) -> dict[str, Any] | None:
        """处理一条 ASR sentence，返回需要广播的字幕事件。"""
        if not isinstance(sentence, dict) or sentence.get("heartbeat"):
            return None
        text = str(sentence.get("text") or "").strip()
        sentence_id = sentence.get("sentence_id")
        sentence_end = bool(sentence.get("sentence_end"))
        sentence_begin = bool(sentence.get("sentence_begin"))

        with self._lock:
            if sentence_begin:
                self._partial = ""
            if sentence_end:
                # sentence_id 在文档里是整数，但云端/json 也可能给字符串。
                # 统一转成字符串，避免所有句子都落到同一个 key 而被当成重复。
                sid = str(sentence_id) if sentence_id is not None else text
                key = (self._session, sid)
                if text and key not in self._finalized:
                    self._finals.append(text)
                    self._finalized.add(key)
                self._partial = ""
            else:
                # 中间结果里偶发空文本（句首通知），不要用它覆盖已显示的内容。
                if text:
                    self._partial = text
            changed = sentence_begin or sentence_end or bool(text)
            if not changed:
                return None
            self._last_update = time.time()
            event = {
                "type": "caption",
                "finals": list(self._finals)[-8:],
                "partial": self._partial,
                "last_update": self._last_update,
            }
        return event if emit else None


def dumps_event(event: dict[str, Any]) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n\n"
