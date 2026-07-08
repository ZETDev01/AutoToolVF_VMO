from __future__ import annotations

from http.server import BaseHTTPRequestHandler

from api._json import send_json, send_options


class handler(BaseHTTPRequestHandler):
    def do_OPTIONS(self):
        send_options(self)

    def do_GET(self):
        send_json(
            self,
            200,
            {
                "listening": False,
                "transcripts": [],
                "last_error": "Mic backend không chạy trên Vercel serverless",
                "runtime": "vercel-python",
            },
        )

    def do_POST(self):
        self.do_GET()
