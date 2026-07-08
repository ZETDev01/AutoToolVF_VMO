import asyncio
import logging
import os

from .common import CHUNK_SIZE, PCM16, RATE_IN, pya

DEFAULT_INPUT_DEVICE_INDEX = 12
INPUT_DEVICE_ENV = "AUDIO_INPUT_DEVICE_INDEX"
INPUT_DEVICE_NAME_PRIORITY = ("pipewire", "pulse", "default")


def _device_info(index: int):
    try:
        return pya.get_device_info_by_index(index)
    except Exception:
        return None


def _has_input(info: dict | None):
    return bool(info and int(info.get("maxInputChannels", 0)) > 0)


def _add_candidate(candidates: list[int], index: int | None):
    if index is None or index in candidates:
        return
    if _has_input(_device_info(index)):
        candidates.append(index)


def _input_device_candidates(fallback_index: int = DEFAULT_INPUT_DEVICE_INDEX):
    candidates = []

    env_value = os.getenv(INPUT_DEVICE_ENV)
    if env_value:
        try:
            _add_candidate(candidates, int(env_value))
        except ValueError:
            logging.warning("Invalid %s=%r; ignoring.", INPUT_DEVICE_ENV, env_value)

    for preferred_name in INPUT_DEVICE_NAME_PRIORITY:
        for index in range(pya.get_device_count()):
            info = _device_info(index)
            name = str(info.get("name", "")).lower() if info else ""
            if preferred_name in name:
                _add_candidate(candidates, index)

    try:
        info = pya.get_default_input_device_info()
        _add_candidate(candidates, int(info["index"]))
    except Exception:
        pass

    _add_candidate(candidates, fallback_index)
    return candidates


def _open_input_stream():
    open_kwargs = dict(
        format=PCM16,
        channels=1,
        rate=RATE_IN,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )
    last_error = None

    for device_index in _input_device_candidates():
        try:
            stream = pya.open(**open_kwargs, input_device_index=device_index)
            print("device_index microphone ==> ", device_index)
            return stream
        except Exception as exc:
            last_error = exc
            logging.warning("Cannot open input_device_index=%s: %s", device_index, exc)

    try:
        stream = pya.open(**open_kwargs)
        print("device_index microphone ==>  system default")
        return stream
    except Exception as exc:
        raise last_error or exc


class AudioIn:
    def __init__(self):
        self._stop = asyncio.Event()
        self.callbacks = []

    async def run(self):
        stream = _open_input_stream()
        try:
            while True:
                chunk = await asyncio.to_thread(stream.read, CHUNK_SIZE)
                if self._stop.is_set():
                    break
                for fn in self.callbacks:
                    await fn(chunk)
        finally:
            stream.close()

    async def stop(self, current_task):
        self._stop.set()
        await current_task
