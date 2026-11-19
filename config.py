"""配置读取、校验与持久化。"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"

DEFAULT_CONFIG: dict[str, Any] = {
    "api_key": "",
    "base_url": "https://dashscope.aliyuncs.com",
    "ws_url": "",
    "model": "qwen-audio-3.0-asr-flash-streaming",
    "language": "zh",
    "audio_source": "loopback",
    "device_index": None,
    "max_sentence_silence": 1000,
    "heartbeat": True,
    "max_task_seconds": 3600,
    "server_host": "127.0.0.1",
    "server_port": 8765,
    "font_size": 64,
    "max_lines": 2,
    "clear_after": 8,
    "bottom": 6,
    "text_color": "#ffffff",
    "box_color": "rgba(0,0,0,0.52)",
    "show_status": False,
    "demo": False,
}

# 浏览器可以修改的字段（服务地址等启动参数不通过网页修改）
EDITABLE_KEYS = {
    "api_key",
    "base_url",
    "ws_url",
    "model",
    "language",
    "audio_source",
    "device_index",
    "max_sentence_silence",
    "heartbeat",
    "max_task_seconds",
    "font_size",
    "max_lines",
    "clear_after",
    "bottom",
    "text_color",
    "box_color",
    "show_status",
    "demo",
}

STYLE_KEYS = (
    "font_size",
    "max_lines",
    "clear_after",
    "bottom",
    "text_color",
    "box_color",
    "show_status",
)

LANGUAGE_CHOICES = ("", "zh", "en", "ja", "ko", "vi", "th", "id", "ms", "tl", "hi", "ar", "fr", "de", "es", "pt", "ru", "it", "nl", "sv", "da", "fi", "no", "el", "pl", "cs", "hu", "ro", "bg", "hr", "sk")


class ConfigError(ValueError):
    """配置值不合法。"""


def _as_int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是整数") from exc


def _as_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ConfigError(f"{name} 必须是数字")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{name} 必须是数字") from exc


def _as_bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "1", "yes", "on"):
        return True
    if isinstance(value, str) and value.lower() in ("false", "0", "no", "off"):
        return False
    raise ConfigError(f"{name} 必须是 true/false")


def _clean_hex_color(value: Any, name: str) -> str:
    text = str(value or "").strip()
    if not text.startswith("#") or len(text) not in (4, 7):
        raise ConfigError(f"{name} 必须是 #RGB 或 #RRGGBB")
    try:
        int(text[1:], 16)
    except ValueError as exc:
        raise ConfigError(f"{name} 不是合法颜色") from exc
    return text


def _clean_box_color(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("#") and len(text) in (4, 7):
        return _clean_hex_color(text, "box_color")
    # 只接受 rgba(...) / rgb(...)，避免把任意 CSS 注入页面
    lowered = text.lower().replace(" ", "")
    if (lowered.startswith("rgba(") or lowered.startswith("rgb(")) and lowered.endswith(")"):
        inner = lowered[lowered.index("(") + 1 : -1]
        parts = inner.split(",")
        if len(parts) in (3, 4):
            try:
                for part in parts:
                    float(part)
            except ValueError:
                pass
            else:
                return text
    raise ConfigError("box_color 必须是 #RRGGBB、rgb(...) 或 rgba(...)")


def validate_patch(patch: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    """校验网页提交的配置片段，返回清洗后的可保存字段。"""
    if not isinstance(patch, dict):
        raise ConfigError("请求体必须是 JSON 对象")
    unknown = set(patch) - EDITABLE_KEYS
    if unknown:
        raise ConfigError(f"不支持的配置项：{', '.join(sorted(unknown))}")

    out: dict[str, Any] = {}
    merged = {**current, **patch}

    if "api_key" in patch:
        out["api_key"] = str(patch["api_key"] or "").strip()
    if "base_url" in patch:
        value = str(patch["base_url"] or "").strip().rstrip("/")
        if not value:
            value = "https://dashscope.aliyuncs.com"
        if not value.startswith(("http://", "https://")):
            raise ConfigError("base_url 必须以 http:// 或 https:// 开头")
        out["base_url"] = value
    if "ws_url" in patch:
        value = str(patch["ws_url"] or "").strip()
        if value and not value.startswith(("ws://", "wss://")):
            raise ConfigError("ws_url 必须以 ws:// 或 wss:// 开头")
        out["ws_url"] = value
    if "model" in patch:
        value = str(patch["model"] or "").strip()
        if not value:
            raise ConfigError("model 不能为空")
        out["model"] = value
    if "language" in patch:
        value = str(patch["language"] or "").strip()
        if value not in LANGUAGE_CHOICES:
            raise ConfigError("不支持的语言代码")
        out["language"] = value
    if "audio_source" in patch:
        value = str(patch["audio_source"] or "").strip()
        if value not in ("loopback", "mic", "device"):
            raise ConfigError("audio_source 只能是 loopback、mic 或 device")
        out["audio_source"] = value
    if "device_index" in patch:
        value = patch["device_index"]
        if value in (None, "", "null"):
            out["device_index"] = None
        else:
            index = _as_int(value, "device_index")
            if index < 0:
                raise ConfigError("device_index 不能为负数")
            out["device_index"] = index
    if "max_sentence_silence" in patch:
        value = _as_int(patch["max_sentence_silence"], "max_sentence_silence")
        out["max_sentence_silence"] = max(200, min(6000, value))
    if "heartbeat" in patch:
        out["heartbeat"] = _as_bool(patch["heartbeat"], "heartbeat")
    if "max_task_seconds" in patch:
        value = _as_int(patch["max_task_seconds"], "max_task_seconds")
        out["max_task_seconds"] = max(0, min(86400, value))
    if "font_size" in patch:
        value = _as_int(patch["font_size"], "font_size")
        out["font_size"] = max(18, min(160, value))
    if "max_lines" in patch:
        value = _as_int(patch["max_lines"], "max_lines")
        out["max_lines"] = max(1, min(4, value))
    if "clear_after" in patch:
        value = _as_float(patch["clear_after"], "clear_after")
        out["clear_after"] = max(0.0, min(120.0, value))
    if "bottom" in patch:
        value = _as_float(patch["bottom"], "bottom")
        out["bottom"] = max(0.0, min(50.0, value))
    if "text_color" in patch:
        out["text_color"] = _clean_hex_color(patch["text_color"], "text_color")
    if "box_color" in patch:
        out["box_color"] = _clean_box_color(patch["box_color"])
    if "show_status" in patch:
        out["show_status"] = _as_bool(patch["show_status"], "show_status")
    if "demo" in patch:
        out["demo"] = _as_bool(patch["demo"], "demo")

    # 跨字段校验
    source = merged.get("audio_source")
    device = merged.get("device_index")
    if source == "device" and device is None:
        raise ConfigError("选择“指定设备”时必须同时选择设备")
    if merged.get("ws_url") and not merged.get("base_url"):
        raise ConfigError("base_url 不能为空")
    return out


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    path = Path(path)
    config = copy.deepcopy(DEFAULT_CONFIG)
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConfigError(f"读取配置文件失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config.json 顶层必须是 JSON 对象")
        for key, value in raw.items():
            if key in DEFAULT_CONFIG:
                config[key] = value
    # 环境变量优先级高于文件，且不会写入 config.json
    if os.environ.get("DASHSCOPE_API_KEY"):
        config["api_key"] = os.environ["DASHSCOPE_API_KEY"].strip()
    if os.environ.get("DASHSCOPE_BASE_URL"):
        config["base_url"] = os.environ["DASHSCOPE_BASE_URL"].strip().rstrip("/")
    return config


def save_config(config: dict[str, Any], path: Path | str = DEFAULT_CONFIG_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {key: config.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    # 环境变量提供的 Key 不落盘
    if os.environ.get("DASHSCOPE_API_KEY"):
        data["api_key"] = ""
    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def effective_api_key(config: dict[str, Any]) -> str:
    return (os.environ.get("DASHSCOPE_API_KEY") or config.get("api_key") or "").strip()


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    """给浏览器看的配置，绝不包含 API Key 明文。"""
    data = {key: config.get(key, DEFAULT_CONFIG[key]) for key in DEFAULT_CONFIG}
    data.pop("api_key", None)
    data["api_key_set"] = bool(effective_api_key(config))
    data["api_key_from_env"] = bool(os.environ.get("DASHSCOPE_API_KEY"))
    return data


def style_config(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config.get(key, DEFAULT_CONFIG[key]) for key in STYLE_KEYS}
