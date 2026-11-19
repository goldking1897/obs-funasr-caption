from __future__ import annotations

import math
import struct
import sys
import tempfile
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import audio_capture


def make_tone(path: str, seconds: float = 2.5, rate: int = 48000) -> None:
    with wave.open(path, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        frames = bytearray()
        for i in range(int(rate * seconds)):
            value = int(12000 * math.sin(2 * math.pi * 440 * i / rate))
            frames += struct.pack("<hh", value, value)
        wf.writeframes(frames)


def main() -> int:
    import winsound

    path = tempfile.mktemp(suffix=".wav")
    make_tone(path)
    winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)

    chunks: list[int] = []
    capture = audio_capture.AudioCapture("loopback", None, lambda data: chunks.append(len(data)))
    device = capture.start()
    print("opened", device.name, device.rate, device.channels, flush=True)
    time.sleep(1.2)
    capture.stop()
    winsound.PlaySound(None, winsound.SND_PURGE)
    print("chunks", len(chunks), "bytes", sum(chunks), "sizes", sorted(set(chunks)), flush=True)
    ok = bool(chunks) and set(chunks) == {len(audio_capture.SILENCE)}
    print("OK" if ok else "FAIL", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
