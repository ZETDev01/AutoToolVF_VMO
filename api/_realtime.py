from __future__ import annotations

import asyncio
import json
import os

from src.voice_client import (
    REALTIME_DEVICE_ID,
    REALTIME_MODEL,
    REALTIME_RESPONSE_MODALITIES,
    connect_bdi,
    realtime_error_message,
)


REQUEST_TIMEOUT_SECONDS = float(os.environ.get("VINFAST_REALTIME_REQUEST_TIMEOUT", "60"))


def realtime_config() -> dict:
    modalities = [
        item.strip()
        for item in os.environ.get("VINFAST_REALTIME_MODALITIES", "text").split(",")
        if item.strip()
    ] or ["text"]
    return {
        "type": "session.update",
        "modalities": modalities,
        "domain": "robot",
        "sample_rate": 16000,
        "session": {
            "modalities": modalities,
            "system_persona": {
                "user_name": "",
                "robot_type": "ambassador",
            },
        },
    }


async def recv_event(conn):
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
    if isinstance(payload, dict) and payload.get("type") and hasattr(conn, "parse_event"):
        return conn.parse_event(data)
    return payload


def event_type(event) -> str:
    if isinstance(event, dict):
        return str(event.get("type") or "")
    return str(getattr(event, "type", "") or "")


def event_delta(event) -> str:
    if isinstance(event, str):
        return event
    if isinstance(event, dict):
        return str(event.get("delta") or event.get("text") or event.get("output") or "")
    return str(getattr(event, "delta", "") or "")


async def ask_realtime(question: str) -> dict:
    manager = connect_bdi()
    conn = None
    chunks: list[str] = []
    try:
        conn = await manager.enter()
        await conn.send(realtime_config())
        await conn.conversation.item.create(
            item={
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": question}],
            }
        )
        await conn.response.create(response={"modalities": REALTIME_RESPONSE_MODALITIES})

        while True:
            event = await asyncio.wait_for(recv_event(conn), timeout=REQUEST_TIMEOUT_SECONDS)
            kind = event_type(event)
            if isinstance(event, str):
                text = event.strip()
                if text:
                    chunks.append(text)
                    break
            elif kind in ("response.text.delta", "response.audio_transcript.delta"):
                chunks.append(event_delta(event))
            elif kind == "response.done":
                break

        return {
            "input": question,
            "output": "".join(chunks).strip(),
            "device_id": REALTIME_DEVICE_ID,
            "model": REALTIME_MODEL,
            "runtime": "vercel-python",
        }
    except Exception as exc:
        return {
            "input": question,
            "output": "",
            "error": realtime_error_message(exc),
            "device_id": REALTIME_DEVICE_ID,
            "model": REALTIME_MODEL,
            "runtime": "vercel-python",
        }
    finally:
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
