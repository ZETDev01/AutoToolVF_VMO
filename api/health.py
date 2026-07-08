from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler

from api._json import send_json, send_options
from src.voice_client import REALTIME_DEVICE_ID, REALTIME_MODEL


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_options(self)

    def do_GET(self):
        configured = bool(os.environ.get("VINFAST_REALTIME_API_KEY"))
        send_json(
            self,
            200,
            {
                "connected": configured,
                "warming": False,
                "sleeping": not configured,
                "last_error": "" if configured else "Thiếu VINFAST_REALTIME_API_KEY trên Vercel",
                "batch_workers": 0,
                "device_id": REALTIME_DEVICE_ID,
                "model": REALTIME_MODEL,
                "modalities": [
                    item.strip()
                    for item in os.environ.get("VINFAST_REALTIME_MODALITIES", "text").split(",")
                    if item.strip()
                ]
                or ["text"],
                "mic": {
                    "listening": False,
                    "transcripts": [],
                    "last_error": "Mic backend không chạy trên Vercel serverless",
                },
                "runtime": "vercel-python",
            },
        )
