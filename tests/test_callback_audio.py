from __future__ import annotations

import math
import struct
import tempfile
import threading
import time
import wave

import pyaudiowpatch as pyaudio


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


path = tempfile.mktemp(suffix=".wav")
make_tone(path)

import winsound

winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)

pa = pyaudio.PyAudio()
info = pa.get_default_wasapi_loopback()
print("device", info["index"], info["name"], info["defaultSampleRate"], info["maxInputChannels"], flush=True)
count = 0
bytes_count = 0


def callback(in_data, frame_count, time_info, status):
    global count, bytes_count
    count += 1
    bytes_count += len(in_data)
    return (None, pyaudio.paContinue)


stream = pa.open(
    format=pyaudio.paInt16,
    channels=min(2, info["maxInputChannels"]),
    rate=int(info["defaultSampleRate"]),
    input=True,
    input_device_index=info["index"],
    frames_per_buffer=4800,
    stream_callback=callback,
)
stream.start_stream()
time.sleep(1.6)
print("callback count", count, "bytes", bytes_count, flush=True)
stream.stop_stream()
stream.close()
pa.terminate()
winsound.PlaySound(None, winsound.SND_PURGE)
print("closed", flush=True)
