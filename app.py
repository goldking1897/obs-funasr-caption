"""把配置、音频采集、ASR 客户端和本地 HTTP 服务粘合在一起。"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

from asr_client import ASRClient, build_ws_url
from audio_capture import AudioCapture, AudioError, list_devices
from config import (
    DEFAULT_CONFIG_PATH,
    STYLE_KEYS,
    ConfigError,
    effective_api_key,
    load_config,
    public_config,
    save_config,
    style_config,
    validate_patch,
)
from demo import DemoSource
from events import CaptionState, EventBus

LOG = logging.getLogger("app")


class App:
    def __init__(self, config_path: Path | str = DEFAULT_CONFIG_PATH) -> None:
        self.config_path = Path(config_path)
        self.config = load_config(self.config_path)
        if not self.config_path.exists():
            # 首次运行自动生成 config.json，省去手动复制示例文件。
            try:
                save_config(self.config, self.config_path)
            except OSError as exc:
                LOG.warning("无法写入配置文件 %s：%s", self.config_path, exc)
        self.bus = EventBus()
        self.state = CaptionState()
        self.started_at = time.time()
        self._lock = threading.RLock()
        self._status = "starting"
        self._message = "正在启动…"
        self._last_error = ""
        self._audio: AudioCapture | None = None
        self._asr: ASRClient | None = None
        self._demo: DemoSource | None = None
        self._audio_info: dict[str, Any] = {}
        self._running = False

    # ---------- 事件 ----------

    def publish(self, event: dict[str, Any]) -> None:
        kind = event.get("type")
        if kind == "status":
            self._status = str(event.get("state") or "unknown")
            self._message = str(event.get("message") or "")
        elif kind == "error":
            self._last_error = str(event.get("message") or "")
        self.bus.publish(event)

    def _publish_status(self, status: str, message: str = "") -> None:
        self.publish({"type": "status", "state": status, "message": message})

    # ---------- 生命周期 ----------

    def start(self) -> None:
        with self._lock:
            if self._running:
                return
            self._running = True
            self._start_pipeline()

    def stop(self) -> None:
        with self._lock:
            self._running = False
            self._stop_pipeline()

    def restart(self, clear: bool = True) -> None:
        with self._lock:
            self._stop_pipeline()
            if clear:
                self.state.reset()
                self.bus.publish({"type": "clear"})
            self._last_error = ""
            self._running = True
            self._start_pipeline()

    def _stop_pipeline(self) -> None:
        if self._audio is not None:
            try:
                self._audio.stop()
            except Exception:
                LOG.exception("停止音频采集失败")
            self._audio = None
        self._stop_asr()
        if self._demo is not None:
            try:
                self._demo.stop()
            except Exception:
                LOG.exception("停止演示失败")
            self._demo = None
        self._audio_info = {}

    def _stop_asr(self) -> None:
        if self._asr is None:
            return
        try:
            self._asr.stop()
        except Exception:
            LOG.exception("停止 ASR 失败")
        self._asr = None

    def _start_pipeline(self) -> None:
        config = self.config
        if config.get("demo"):
            self._demo = DemoSource(self, self.state)
            self._demo.start()
            return

        key = effective_api_key(config)
        if not key:
            self.publish(
                {
                    "type": "error",
                    "message": "还没有配置 DashScope API Key。请在控制台填写，或设置环境变量 DASHSCOPE_API_KEY。",
                }
            )
            self._publish_status("error", "缺少 API Key")
            return

        self._asr = ASRClient(lambda: self.config, self, self.state, lambda: effective_api_key(self.config))
        self._asr.start()

        capture = AudioCapture(
            source=str(config.get("audio_source") or "loopback"),
            device_index=config.get("device_index"),
            on_chunk=self._asr.feed,
            on_error=self._on_audio_error,
            on_info=self._on_audio_info,
        )
        try:
            device = capture.start()
        except AudioError as exc:
            LOG.error("音频启动失败：%s", exc)
            self._stop_asr()
            self.publish({"type": "error", "message": str(exc)})
            self._publish_status("error", "音频设备打开失败")
            return
        except Exception as exc:
            LOG.exception("音频启动异常")
            self._stop_asr()
            self.publish({"type": "error", "message": f"音频启动失败：{exc}"})
            self._publish_status("error", "音频设备打开失败")
            return
        self._audio = capture
        self._audio_info = {
            "device": device.name,
            "rate": device.rate,
            "channels": device.channels,
            "source": config.get("audio_source"),
        }

    def _on_audio_info(self, info: dict[str, Any]) -> None:
        self._audio_info.update(info)

    def _on_audio_error(self, message: str) -> None:
        self.publish({"type": "error", "message": message})
        self._publish_status("error", "音频采集中断")

    # ---------- 配置 ----------

    def update_config(self, patch: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            clean = validate_patch(patch, self.config)
            if not clean:
                return {"ok": True, "config": public_config(self.config), "restarted": False}
            old = self.config
            merged = {**self.config, **clean}
            save_config(merged, self.config_path)
            self.config = merged

            style_changed = any(key in clean and old.get(key) != merged.get(key) for key in STYLE_KEYS)
            pipeline_keys = [key for key in clean if key not in STYLE_KEYS]
            pipeline_changed = any(old.get(key) != merged.get(key) for key in pipeline_keys)
            if style_changed:
                self.bus.publish({"type": "style", "style": style_config(merged)})
            restarted = False
            if pipeline_changed:
                self.restart(clear=False)
                restarted = True
            return {"ok": True, "config": public_config(self.config), "restarted": restarted}

    def devices(self) -> dict[str, Any]:
        return list_devices()

    def clear(self) -> None:
        with self._lock:
            self.state.reset()
            self.bus.publish({"type": "clear"})

    # ---------- 状态 ----------

    def snapshot(self) -> dict[str, Any]:
        caption = self.state.snapshot()
        asr_status = self._asr.status if self._asr is not None else self._status
        asr_message = self._asr.message if self._asr is not None else self._message
        return {
            "type": "snapshot",
            "status": asr_status,
            "message": asr_message,
            "error": self._last_error,
            "caption": caption,
            "style": style_config(self.config),
            "config": public_config(self.config),
            "ws_url": build_ws_url(self.config),
            "audio": self._audio_info,
            "demo": bool(self.config.get("demo")),
            "uptime": round(time.time() - self.started_at, 1),
        }
