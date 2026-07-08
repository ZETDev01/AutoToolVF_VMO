from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
from typing import TYPE_CHECKING
from urllib.parse import urlencode

from openai import AsyncOpenAI
from websockets.exceptions import ConnectionClosed, ConnectionClosedOK, InvalidStatus

from .outputs import ActionOut, TextOut

if TYPE_CHECKING:
    from .speaker import AudioOut


REALTIME_BASE_URL = os.environ.get(
    "VINFAST_REALTIME_BASE_URL", "wss://groot.vizone.ai/api/v2/s2s"
).rstrip("/")
REALTIME_MODEL = os.environ.get("VINFAST_REALTIME_MODEL", "vsf")
REALTIME_DEVICE_ID = os.environ.get(
    "VINFAST_REALTIME_DEVICE_ID", "robot_03072026_official_qcd"
)
REALTIME_API_KEY = os.environ.get("VINFAST_REALTIME_API_KEY", "")
REALTIME_API_KEY_HEADER = os.environ.get("VINFAST_REALTIME_API_KEY_HEADER", "").strip()
REALTIME_CONNECT_TIMEOUT = float(os.environ.get("VINFAST_REALTIME_CONNECT_TIMEOUT", "15"))
REALTIME_RESPONSE_MODALITIES = [
    item.strip()
    for item in os.environ.get("VINFAST_REALTIME_RESPONSE_MODALITIES", "text").split(",")
    if item.strip()
] or ["text"]
REALTIME_STARTUP_TEXT = os.environ.get("VINFAST_REALTIME_STARTUP_TEXT", "").strip()
CONNECT_RETRY_DELAYS = (2, 5)
DISCONNECT_TIMEOUT = 5


class VoiceClient:
    def __init__(self, config: dict, timeout: float = 20):
        self.awake = asyncio.Event()
        self.text_out: TextOut = None
        self.audio_out: AudioOut = None
        self.action_out: ActionOut = None
        self.transcript_callbacks = []
        self.conn = None
        self.config = config
        self.timeout = timeout
        self.startup_done = False
        self.stopping = False
        self.connected_event = asyncio.Event()
        self.last_error = None
        self.reconnect_count = 0
        self.connected_at = 0.0
        self.last_event_at = 0.0
        self.network_stats = {
            "input_audio_raw_bytes": 0,
            "input_audio_base64_bytes": 0,
            "input_audio_chunks": 0,
            "output_audio_raw_bytes": 0,
            "output_audio_base64_bytes": 0,
            "output_audio_events": 0,
        }

    async def run(self):
        while not self.stopping:
            await self.awake.wait()
            print("[voice] awakening..")
            try:
                conn = await connect_bdi_with_retry()
            except Exception as exc:
                self.last_error = realtime_error_message(exc)
                if realtime_proxy_auth_error(exc):
                    logging.warning("realtime WebSocket blocked: %s", self.last_error)
                else:
                    logging.exception("realtime WebSocket connect failed")
                await asyncio.sleep(realtime_retry_delay(exc))
                continue
            try:
                self.conn = conn
                self.connected_event.set()
                self.last_error = None
                self.reconnect_count += 1
                self.connected_at = time.monotonic()
                self.last_event_at = self.connected_at
                await self.conn.send(self.config)
                if REALTIME_STARTUP_TEXT and not self.startup_done:
                    await self.send_text(REALTIME_STARTUP_TEXT)
                    self.startup_done = True

                while True:
                    event = await asyncio.wait_for(self._recv_event(conn), self.timeout)
                    self.last_event_at = time.monotonic()
                    if isinstance(event, str):
                        text = event.strip()
                        if text and self.text_out:
                            if hasattr(self.text_out, "append_event"):
                                self.text_out.append_event("response.text.delta", text)
                            else:
                                self.text_out.append(text)
                            if hasattr(self.text_out, "response_done"):
                                self.text_out.response_done()
                        continue
                    if event.type not in ["response.text.delta"]:
                        print("event ==> ", event.type)
                    if event.type in (
                        "response.text.delta",
                        "response.audio_transcript.delta",
                    ):
                        if self.text_out:
                            if hasattr(self.text_out, "append_event"):
                                self.text_out.append_event(event.type, event.delta)
                            else:
                                self.text_out.append(event.delta)
                    elif event.type in (
                        "response.text.done",
                        "response.audio_transcript.done",
                    ):
                        if self.text_out:
                            if hasattr(self.text_out, "done_event"):
                                self.text_out.done_event(event.type)
                            else:
                                self.text_out.done()
                    elif event.type == "response.action.done":
                        if self.action_out:
                            self.action_out.action_done(event.action)
                    elif event.type == "response.audio.delta":
                        chunk = base64.b64decode(event.delta)
                        self.network_stats["output_audio_raw_bytes"] += len(chunk)
                        self.network_stats["output_audio_base64_bytes"] += len(event.delta)
                        self.network_stats["output_audio_events"] += 1
                        if self.audio_out:
                            self.audio_out.send(chunk)
                    elif event.type == "response.audio.done":
                        pass
                    elif event.type == "input_audio_buffer.speech_started":
                        print("===== START SPEECH =====")
                        if self.audio_out:
                            self.audio_out.clear()
                    elif event.type == "input_audio_buffer.speech_stopped":
                        print("===== STOP SPEECH =====")
                    elif event.type == "conversation.item.input_audio_transcription.completed":
                        print("YOU: ", event.transcript)
                        for callback in self.transcript_callbacks:
                            callback(event.transcript)
                    elif event.type == "response.done":
                        print("===== RESPONSE DONE =====")
                        if self.text_out and hasattr(self.text_out, "response_done"):
                            self.text_out.response_done()
            except (ConnectionClosedOK, asyncio.TimeoutError) as exc:
                if isinstance(exc, asyncio.TimeoutError):
                    self.last_error = f"Realtime idle timeout after {int(self.timeout)} seconds"
                if self.text_out and hasattr(self.text_out, "fail_response_waiters"):
                    self.text_out.fail_response_waiters(
                        ConnectionError(self.last_error or "Realtime connection closed")
                    )
            except ConnectionClosed as exc:
                self.last_error = realtime_error_message(exc)
                if self.text_out and hasattr(self.text_out, "fail_response_waiters"):
                    self.text_out.fail_response_waiters(
                        ConnectionError(self.last_error or "Realtime connection closed")
                    )
            except Exception as exc:
                self.last_error = realtime_error_message(exc)
                logging.exception("realtime receive loop failed")
                if self.text_out and hasattr(self.text_out, "fail_response_waiters"):
                    self.text_out.fail_response_waiters(exc)
            finally:
                self.connected_event.clear()
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(conn.close(), timeout=DISCONNECT_TIMEOUT)
                self.conn = None
                if self.stopping:
                    break
                print("[voice] reconnecting..")
                await asyncio.sleep(1)

    async def send_text(self, text: str):
        if not self.conn:
            print("[send_text] asleeping.")
            return

        if self.text_out and hasattr(self.text_out, "start_input"):
            self.text_out.start_input(text)
        await self.conn.conversation.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": text}],
            }
        )
        await self.conn.response.create(response={"modalities": REALTIME_RESPONSE_MODALITIES})

    async def _recv_event(self, conn):
        if not hasattr(conn, "recv_bytes"):
            return await conn.recv()

        data = await conn.recv_bytes()
        raw = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data
        if not isinstance(raw, str):
            return raw

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return raw

        if isinstance(payload, str):
            return payload
        if isinstance(payload, dict) and payload.get("type") and hasattr(conn, "parse_event"):
            return conn.parse_event(data)

        text = _plain_text_from_payload(payload)
        return text if text is not None else raw

    async def send_audio(self, chunk: bytes):
        if not self.conn:
            print("?", end="", flush=True)
            return

        item = base64.b64encode(chunk).decode()
        self.network_stats["input_audio_raw_bytes"] += len(chunk)
        self.network_stats["input_audio_base64_bytes"] += len(item)
        self.network_stats["input_audio_chunks"] += 1
        await self.conn.input_audio_buffer.append(audio=item)

    async def startup(self):
        await self.conn.send(self.config)
        await self.send_text('xin chào')

    async def connect(self, force: bool = False):
        if self.conn and force:
            print("[connect] already awaken.")
            conn = self.conn
            self.conn = None
            self.connected_event.clear()
            await conn.close()
        elif self.conn:
            self.connected_event.set()
            return

        self.awake.set()

    async def wait_connected(self, timeout: float = 45):
        if self.conn:
            self.connected_event.set()
            return True
        self.awake.set()
        try:
            await asyncio.wait_for(self.connected_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self.conn)

    async def disconnect(self):
        if not self.conn:
            print("[disconnect] already aslept.")
            return

        await asyncio.wait_for(self.conn.close(), timeout=DISCONNECT_TIMEOUT)

    async def sleep(self):
        self.awake.clear()
        self.connected_event.clear()
        if not self.conn:
            print("[sleep] already aslept.")
            return
        conn = self.conn
        self.conn = None
        await asyncio.wait_for(conn.close(), timeout=DISCONNECT_TIMEOUT)


def connect_bdi():
    client = AsyncOpenAI(api_key=REALTIME_API_KEY, websocket_base_url=REALTIME_BASE_URL)
    return client.beta.realtime.connect(
        model=REALTIME_MODEL,
        extra_query={
            # "device_id": "robot_03072026_official_qcd",
            "device_id": REALTIME_DEVICE_ID,
        },
        extra_headers=realtime_extra_headers(),
        websocket_connection_options={"open_timeout": REALTIME_CONNECT_TIMEOUT},
    )


async def connect_bdi_with_retry():
    last_error = None
    for attempt, delay in enumerate((0, *CONNECT_RETRY_DELAYS), start=1):
        if delay:
            await asyncio.sleep(delay)

        manager = connect_bdi()
        logging.info(
            "realtime WebSocket connecting attempt=%s url=%s",
            attempt,
            realtime_connect_url(),
        )
        try:
            return await manager.enter()
        except Exception as exc:
            last_error = exc
            logging.warning(
                "realtime WebSocket connect failed attempt=%s error=%s",
                attempt,
                realtime_error_message(exc),
            )

    raise last_error


def realtime_connect_url():
    query = urlencode({"model": REALTIME_MODEL, "device_id": REALTIME_DEVICE_ID})
    return f"{REALTIME_BASE_URL}/realtime?{query}"


def realtime_extra_headers():
    if not REALTIME_API_KEY_HEADER:
        return {}
    return {REALTIME_API_KEY_HEADER: REALTIME_API_KEY}


def realtime_error_message(exc: Exception) -> str:
    text = str(exc)
    if realtime_proxy_auth_error(exc):
        return (
            "Company proxy redirected realtime WebSocket to an authentication page. "
            "Authenticate with the proxy or ask IT to allowlist groot.vizone.ai:443 for WebSocket."
        )
    status = realtime_http_status(exc)
    if status in (401, 403):
        header_hint = (
            f" If Kong requires key-auth, try setting "
            f"VINFAST_REALTIME_API_KEY_HEADER=apikey before running."
            if not REALTIME_API_KEY_HEADER
            else f" Current key header: {REALTIME_API_KEY_HEADER}."
        )
        return (
            f"Realtime server rejected WebSocket with HTTP {status}. "
            f"Check VINFAST_REALTIME_API_KEY and VINFAST_REALTIME_DEVICE_ID "
            f"(current device_id={REALTIME_DEVICE_ID}).{header_hint}"
        )
    return text or "realtime WebSocket connect failed"


def realtime_http_status(exc: Exception) -> int | None:
    if isinstance(exc, InvalidStatus):
        return getattr(exc.response, "status_code", None)
    return None


def realtime_proxy_auth_error(exc: Exception) -> bool:
    text = str(exc)
    return (
        "mwg-internal" in text
        or "target=Auth" in text
        or "server rejected WebSocket connection: HTTP 302" in text
    )


def realtime_retry_delay(exc: Exception) -> int:
    if realtime_proxy_auth_error(exc) or realtime_http_status(exc) in (401, 403):
        return 60
    return 3


def _plain_text_from_payload(payload) -> str | None:
    if isinstance(payload, str):
        return payload
    if not isinstance(payload, dict):
        return None

    for key in ("text", "output", "response", "message", "answer", "content"):
        value = payload.get(key)
        if isinstance(value, str):
            return value

    data = payload.get("data")
    if isinstance(data, dict):
        return _plain_text_from_payload(data)
    return None
