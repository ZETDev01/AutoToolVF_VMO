import asyncio
import logging
import os
import time
import numpy as np
from .common import CHUNK_SIZE, PCM16, RATE_OUT, pya

DEFAULT_OUTPUT_DEVICE_INDEX = 12
OUTPUT_DEVICE_ENV = "AUDIO_OUTPUT_DEVICE_INDEX"
OUTPUT_DEVICE_NAME_PRIORITY = ("pipewire", "pulse", "default")
SPEAKER_WARMUP_SECONDS = 0.2

_RMS_TRIGGER = 8000
_TARGET_PEAK = 24000


def _normalize_if_loud(chunk: bytes) -> bytes:
    """Scale down a PCM16 chunk only when amplitude is abnormally high."""
    print(time.time(), "write chunk", len(chunk), len(chunk) / (RATE_OUT * 2), type(chunk), chunk[:10])
    if len(chunk) % 2 != 0:
        chunk = chunk[:-1]
    if not chunk:
        return chunk
    try:
        samples = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
        rms = np.sqrt(np.mean(samples ** 2))
        if rms < _RMS_TRIGGER:
            return chunk
        # print("rms > _RMS_TRIGGER", rms)
        peak = np.max(np.abs(samples))
        if peak == 0:
            print(time.time(), "===================== 0", chunk[:10])
            return chunk
        print(time.time(), "===================== peak > 0", peak, chunk[:10])

        scale = _TARGET_PEAK / peak
        scaled = np.clip(samples * scale, -32768, 32767).astype(np.int16)
        return scaled.tobytes()
    except Exception as e:
        print("Error normalizing chunk:", e)
        return chunk

def _device_info(index: int):
    try:
        return pya.get_device_info_by_index(index)
    except Exception:
        return None


def _has_output(info: dict | None):
    return bool(info and int(info.get("maxOutputChannels", 0)) > 0)


def _add_candidate(candidates: list[int], index: int | None):
    if index is None or index in candidates:
        return
    if _has_output(_device_info(index)):
        candidates.append(index)


def _output_device_candidates(fallback_index: int = DEFAULT_OUTPUT_DEVICE_INDEX):
    candidates = []

    env_value = os.getenv(OUTPUT_DEVICE_ENV)
    if env_value:
        try:
            _add_candidate(candidates, int(env_value))
        except ValueError:
            logging.warning("Invalid %s=%r; ignoring.", OUTPUT_DEVICE_ENV, env_value)

    for preferred_name in OUTPUT_DEVICE_NAME_PRIORITY:
        for index in range(pya.get_device_count()):
            info = _device_info(index)
            name = str(info.get("name", "")).lower() if info else ""
            if preferred_name in name:
                _add_candidate(candidates, index)

    try:
        info = pya.get_default_output_device_info()
        _add_candidate(candidates, int(info["index"]))
    except Exception:
        pass

    _add_candidate(candidates, fallback_index)
    return candidates


def _open_output_stream():
    open_kwargs = dict(
        format=PCM16,
        channels=1,
        rate=RATE_OUT,
        output=True,
        frames_per_buffer=CHUNK_SIZE,
    )
    last_error = None

    for device_index in _output_device_candidates():
        try:
            stream = pya.open(**open_kwargs, output_device_index=device_index)
            print("device_index speaker ==> ", device_index)
            return stream
        except Exception as exc:
            last_error = exc
            logging.warning("Cannot open output_device_index=%s: %s", device_index, exc)

    try:
        stream = pya.open(**open_kwargs)
        print("device_index speaker ==>  system default")
        return stream
    except Exception as exc:
        raise last_error or exc


def _warmup_silence():
    frames = int(RATE_OUT * SPEAKER_WARMUP_SECONDS)
    return b"\x00\x00" * frames


class AudioOut:
    def __init__(self):
        self._queue = asyncio.Queue()
        self._ready = asyncio.Event()
        self._startup_error = None
        self.send_callbacks = []

    async def run(self):
        stream = None
        try:
            stream = _open_output_stream()
            started = time.time()
            await asyncio.to_thread(stream.write, _warmup_silence())
            print(time.time(), "speaker warmup done", time.time() - started)
            self._ready.set()

            while True:
                chunk = await self._queue.get()
                await asyncio.to_thread(stream.write, chunk)
        except Exception as exc:
            self._startup_error = exc
            self._ready.set()
            raise
        finally:
            if stream:
                stream.close()

    async def wait_ready(self):
        await self._ready.wait()
        if self._startup_error:
            raise self._startup_error

    def send(self, chunk: bytes):
        # chunk = _normalize_if_loud(chunk)
        if len(chunk) % 2 != 0:
            chunk = chunk[:-1]
        for fn in self.send_callbacks:
            fn(chunk)
        self._queue.put_nowait(chunk)

    def clear(self):
        while not self._queue.empty():
            self._queue.get_nowait()
