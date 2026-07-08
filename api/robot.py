from __future__ import annotations

import os
from http.server import BaseHTTPRequestHandler

from api._json import send_json, send_options


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_options(self)

    def do_POST(self):
        configured = bool(os.environ.get("VINFAST_REALTIME_API_KEY"))
        send_json(
            self,
            200 if configured else 400,
            {
                "ok": configured,
                "connected": configured,
                "sleeping": not configured,
                "message": (
                    "Vercel serverless sẽ kết nối realtime khi gửi từng câu hỏi"
                    if configured
                    else "Thiếu VINFAST_REALTIME_API_KEY trên Vercel"
                ),
                "runtime": "vercel-python",
            },
        )
