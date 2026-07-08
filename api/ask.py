from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler

from api._json import read_json_body, send_json, send_options
from api._realtime import ask_realtime


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_options(self)

    def do_POST(self):
        try:
            payload = read_json_body(self)
            question = str(payload.get("question") or "").strip()
            if not question:
                send_json(self, 400, {"error": "Thiếu câu hỏi"})
                return

            result = asyncio.run(ask_realtime(question[:2000]))
            send_json(self, 200 if result.get("output") else 502, result)
        except Exception as exc:
            send_json(self, 500, {"error": str(exc), "runtime": "vercel-python"})
