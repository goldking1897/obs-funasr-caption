"""Windows 音频采集，输出实时 ASR 需要的 16 kHz / 16-bit / 单声道 PCM。"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np
import pyaudiowpatch as pyaudio

LOG = logging.getLogger("audio")
TARGET_RATE = 16000
CHUNK_SECONDS = 0.1
SILENCE = b"\x00\x00" * int(TARGET_RATE * CHUNK_SECONDS)


class AudioError(RuntimeError):
    pass


@dataclass
class Device:
    index: int
    name: str
    rate: int
    channels: int
    loopback: bool
    default: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "name": self.name,
            "rate": self.rate,
            "channels": self.channels,
            "loopback": self.loopback,
            "default": self.default,
        }


def _wasapi_index(pa: pyaudio.PyAudio) -> int:
    return int(pa.get_host_api_info_by_type(pyaudio.paWASAPI)["index"])


def _device_to_model(info: dict[str, Any], default: bool = False) -> Device:
    return Device(
        index=int(info["index"]),
        name=str(info.get("name") or f"设备 {info['index']}"),
        rate=int(round(float(info.get("defaultSampleRate") or 48000))),
        channels=max(1, int(info.get("maxInputChannels") or 1)),
        loopback=bool(info.get("isLoopbackDevice")),
        default=default,
    )


def list_devices() -> dict[str, Any]:
    """列出 WASAPI 回环设备与麦克风设备。"""
    pa = pyaudio.PyAudio()
    try:
        wasapi = _wasapi_index(pa)
        default_loopback = None
        default_mic = None
        try:
            default_loopback = int(pa.get_default_wasapi_loopback()["index"])
        except Exception:
            pass
        try:
            host = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_mic = int(host["defaultInputDevice"])
            if default_mic < 0:
                default_mic = None
        except Exception:
            pass

        loopbacks: list[Device] = []
        mics: list[Device] = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if int(info.get("hostApi", -1)) != wasapi:
                continue
            if info.get("isLoopbackDevice") and int(info.get("maxInputChannels") or 0) > 0:
                loopbacks.append(_device_to_model(info, i == default_loopback))
            elif int(info.get("maxInputChannels") or 0) > 0:
                mics.append(_device_to_model(info, i == default_mic))
        return {
            "loopback": [d.as_dict() for d in loopbacks],
            "mic": [d.as_dict() for d in mics],
            "default_loopback": default_loopback,
            "default_mic": default_mic,
        }
    finally:
        pa.terminate()


def _resolve_device(pa: pyaudio.PyAudio, source: str, wanted_index: int | None) -> dict[str, Any]:
    if source == "loopback":
        if wanted_index is not None:
            info = pa.get_device_info_by_index(wanted_index)
            if not info.get("isLoopbackDevice"):
                raise AudioError(f"设备 {wanted_index} 不是 WASAPI 回环设备")
            return info
        try:
            return pa.get_default_wasapi_loopback()
        except Exception as exc:
            raise AudioError("找不到默认扬声器回环设备，请确认系统有正在使用的播放设备") from exc

    if source == "mic":
        if wanted_index is not None:
            info = pa.get_device_info_by_index(wanted_index)
        else:
            host = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            index = int(host["defaultInputDevice"])
            if index < 0:
                raise AudioError("系统没有默认麦克风")
            info = pa.get_device_info_by_index(index)
        if int(info.get("maxInputChannels") or 0) <= 0:
            raise AudioError(f"设备 {info.get('name')} 不是输入设备")
        return info

    if source == "device":
        if wanted_index is None:
            raise AudioError("没有选择音频设备")
        info = pa.get_device_info_by_index(wanted_index)
        if int(info.get("maxInputChannels") or 0) <= 0:
            raise AudioError(f"设备 {info.get('name')} 不是输入设备")
        return info

    raise AudioError(f"未知音频源：{source}")


def to_pcm16k(data: bytes, rate: int, channels: int) -> bytes:
    """把任意采样率/声道数的 int16 PCM 转为 16 kHz 单声道。"""
    audio = np.frombuffer(data, dtype=np.int16)
    if audio.size == 0:
        return b""
    if channels > 1:
        usable = (audio.size // channels) * channels
        audio = audio[:usable].reshape(-1, channels).astype(np.float32).mean(axis=1)
    else:
        audio = audio.astype(np.float32)

    if rate != TARGET_RATE:
        if rate % TARGET_RATE == 0:
            factor = rate // TARGET_RATE
            usable = (audio.size // factor) * factor
            if usable:
                audio = audio[:usable].reshape(-1, factor).mean(axis=1)
        else:
            # 对 44.1 kHz 等非整数倍采样率做线性插值；语音识别足够用。
            target_len = max(1, int(round(audio.size * TARGET_RATE / rate)))
            src_x = np.linspace(0.0, 1.0, audio.size, endpoint=False)
            dst_x = np.linspace(0.0, 1.0, target_len, endpoint=False)
            audio = np.interp(dst_x, src_x, audio)

    audio = np.clip(np.rint(audio), -32768, 32767).astype(np.int16)
    return audio.tobytes()


class AudioCapture:
    """用 PortAudio 回调模式采集音频，按 100 ms 一块回调。

    注意：部分 Windows 声卡在系统完全静音时不会触发 WASAPI 回环回调，
    阻塞式 read 则会一直卡住。这里用回调模式避免卡死，并在检测到声音后
    补发一段静音，让云端 VAD 能正常断句。
    """

    # 最后一次真实声音之后补发静音的时长（秒）；避免长时间空闲也持续计费。
    SILENCE_TAIL_SECONDS = 3.0

    def __init__(
        self,
        source: str,
        device_index: int | None,
        on_chunk: Callable[[bytes], None],
        on_error: Callable[[str], None] | None = None,
        on_info: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.source = source
        self.device_index = device_index
        self.on_chunk = on_chunk
        self.on_error = on_error or (lambda _msg: None)
        self.on_info = on_info or (lambda _info: None)
        self._stop = threading.Event()
        self._pa: pyaudio.PyAudio | None = None
        self._stream: Any = None
        self._ticker: threading.Thread | None = None
        self._rate = 48000
        self._channels = 1
        self._device: Device | None = None
        self._buffer = bytearray()
        self._emit_lock = threading.Lock()
        self._last_audio_at = 0.0
        self._last_callback_at = 0.0
        self._last_emit_at = 0.0
        self._saw_audio = False
        self._failure: str | None = None

    @property
    def device(self) -> Device | None:
        return self._device

    def start(self) -> Device:
        """同步打开设备，失败时直接抛 AudioError。"""
        pa = pyaudio.PyAudio()
        info = _resolve_device(pa, self.source, self.device_index)
        rate = int(round(float(info.get("defaultSampleRate") or 48000)))
        channels = max(1, int(info.get("maxInputChannels") or 1))
        # 多声道设备（如 5.1）会把人声放在中置声道，保留全部声道做平均，
        # 避免只取前两声道时漏掉中置人声；最多 8 声道，防止异常设备。
        channels = min(channels, 8)
        frames = max(1, int(round(rate * 0.02)))
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=int(info["index"]),
                frames_per_buffer=frames,
                stream_callback=self._callback,
            )
            stream.start_stream()
        except Exception as exc:
            try:
                pa.terminate()
            except Exception:
                pass
            raise AudioError(
                f"无法打开音频设备「{info.get('name')}」：{exc}\n"
                "提示：关闭占用该设备的程序，或在控制台换一个设备。"
            ) from exc

        self._pa = pa
        self._stream = stream
        self._rate = rate
        self._channels = channels
        self._buffer.clear()
        self._stop.clear()
        self._saw_audio = False
        self._failure = None
        self._last_audio_at = 0.0
        self._last_callback_at = 0.0
        self._last_emit_at = 0.0
        self._device = _device_to_model(info)
        self._ticker = threading.Thread(target=self._silence_ticker, name="audio-silence", daemon=True)
        self._ticker.start()
        LOG.info("音频采集启动：%s (%s Hz, %s ch)", self._device.name, rate, channels)
        self.on_info({"device": self._device.name, "rate": rate, "channels": channels})
        return self._device

    def _callback(self, in_data: bytes, frame_count: int, time_info: Any, status: int) -> tuple[None, int]:
        if self._stop.is_set():
            return (None, pyaudio.paComplete)
        now = time.monotonic()
        self._last_callback_at = now
        try:
            pcm = to_pcm16k(in_data, self._rate, self._channels)
        except Exception as exc:
            LOG.exception("音频格式转换失败")
            self._failure = f"音频格式转换失败：{exc}"
            return (None, pyaudio.paContinue)
        if not pcm:
            return (None, pyaudio.paContinue)

        # 有些驱动在静音时仍然持续回调并送全零数据。只把有信号的数据
        # 当作“正在说话”，否则一直往云端送静音会白白占用识别时长。
        peak = int(np.max(np.abs(np.frombuffer(pcm, dtype=np.int16).astype(np.int32))))
        if peak > 8:
            self._last_audio_at = now
            self._saw_audio = True
        elif not self._saw_audio:
            return (None, pyaudio.paContinue)
        elif now - self._last_audio_at > self.SILENCE_TAIL_SECONDS:
            # 尾音静音已经由 ticker 补过，后续全零帧不再发送。
            self._buffer.clear()
            return (None, pyaudio.paContinue)

        self._buffer.extend(pcm)
        with self._emit_lock:
            while len(self._buffer) >= len(SILENCE):
                chunk = bytes(self._buffer[: len(SILENCE)])
                del self._buffer[: len(SILENCE)]
                self._emit(chunk)
        return (None, pyaudio.paContinue)

    def _emit(self, chunk: bytes) -> None:
        self._last_emit_at = time.monotonic()
        try:
            self.on_chunk(chunk)
        except Exception:
            LOG.exception("音频回调处理失败")

    def _silence_ticker(self) -> None:
        """声音停止后补发几秒静音；系统静音时 WASAPI 回环不会产生任何数据。"""
        while not self._stop.wait(0.1):
            if not self._saw_audio:
                continue
            now = time.monotonic()
            # 回调仍在持续（哪怕送的是静音）时，由回调按真实时钟推进；
            # ticker 只负责“设备完全不再回调”的情况，避免重复补静音。
            if now - self._last_callback_at < 0.3:
                continue
            idle = now - self._last_audio_at
            if idle > self.SILENCE_TAIL_SECONDS:
                continue
            with self._emit_lock:
                if now - self._last_emit_at >= CHUNK_SECONDS * 0.9:
                    self._emit(SILENCE)

    def stop(self) -> None:
        self._stop.set()
        ticker = self._ticker
        if ticker is not None and ticker.is_alive():
            ticker.join(timeout=1.5)
        self._ticker = None
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop_stream()
            except Exception:
                pass
            try:
                stream.close()
            except Exception:
                pass
        pa, self._pa = self._pa, None
        if pa is not None:
            try:
                pa.terminate()
            except Exception:
                pass
        self._buffer.clear()
        LOG.info("音频采集已停止")
        if self._failure:
            self.on_error(self._failure)
            self._failure = None

    @property
    def healthy(self) -> bool:
        return self._failure is None


__all__ = ["AudioCapture", "AudioError", "Device", "SILENCE", "list_devices", "to_pcm16k"]
