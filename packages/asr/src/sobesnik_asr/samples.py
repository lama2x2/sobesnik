"""Крошечные аудиофайлы для тестов (как `FakeTranscriber`): генерируются на лету,
в репозитории аудио нет."""

import io
import math
import wave

import av
import numpy as np


def wav_bytes(seconds: float = 1.5, rate: int = 16000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        n = int(seconds * rate)
        tone = (int(8000 * math.sin(2 * math.pi * 440 * i / rate)) for i in range(n))
        out.writeframes(b"".join(v.to_bytes(2, "little", signed=True) for v in tone))
    return buffer.getvalue()


def ogg_opus_bytes(seconds: float = 1.5, rate: int = 48000) -> bytes:
    """ogg/opus — как голосовое Telegram."""
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="ogg") as container:
        stream = container.add_stream("libopus", rate=rate)
        stream.layout = "mono"
        frame_size = 960
        total = int(seconds * rate)
        t = np.arange(total, dtype=np.float32) / rate
        samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        for start in range(0, total, frame_size):
            chunk = samples[start : start + frame_size].reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(chunk, format="flt", layout="mono")
            frame.rate = rate
            frame.pts = start
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    return buffer.getvalue()


def mp4_video_bytes(seconds: float = 1.5, rate: int = 48000) -> bytes:
    """mp4 с видео и звуком aac — как кружочек Telegram (видео — чёрный квадрат)."""
    buffer = io.BytesIO()
    with av.open(buffer, mode="w", format="mp4") as container:
        video = container.add_stream("mpeg4", rate=10)
        video.width = video.height = 64
        video.pix_fmt = "yuv420p"
        audio = container.add_stream("aac", rate=rate)
        audio.layout = "mono"
        black = np.zeros((64, 64, 3), dtype=np.uint8)
        for i in range(int(seconds * 10)):
            picture = av.VideoFrame.from_ndarray(black, format="rgb24")
            picture.pts = i
            for packet in video.encode(picture):
                container.mux(packet)
        for packet in video.encode(None):
            container.mux(packet)
        frame_size = 1024
        total = int(seconds * rate)
        t = np.arange(total + frame_size, dtype=np.float32) / rate
        samples = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        for start in range(0, total, frame_size):
            chunk = samples[start : start + frame_size].reshape(1, -1)
            frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="mono")
            frame.rate = rate
            frame.pts = start
            for packet in audio.encode(frame):
                container.mux(packet)
        for packet in audio.encode(None):
            container.mux(packet)
    return buffer.getvalue()
