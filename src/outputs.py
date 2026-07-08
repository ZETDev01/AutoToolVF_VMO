import asyncio


class TextOut:
    def __init__(self):
        self.input_text = ""
        self._text_chunks = []
        self._transcript_chunks = []
        self._response_waiters = []

    def start_input(self, text: str):
        self.input_text = text
        self._text_chunks.clear()
        self._transcript_chunks.clear()

    def append(self, delta: str):
        self._text_chunks.append(delta)

    def append_event(self, event_type: str, delta: str):
        if event_type == "response.audio_transcript.delta":
            self._transcript_chunks.append(delta)
        else:
            self._text_chunks.append(delta)

    def done(self):
        pass

    def done_event(self, event_type: str):
        pass

    def response_done(self):
        response = {"input": self.input_text, "output": self.text}
        print(f"input: {response['input']}, output: {response['output']}")
        while self._response_waiters:
            waiter = self._response_waiters.pop(0)
            if not waiter.done():
                waiter.set_result(response)

    def prepare_response_waiter(self):
        waiter = asyncio.get_running_loop().create_future()
        self._response_waiters.append(waiter)
        return waiter

    def cancel_response_waiter(self, waiter):
        if waiter in self._response_waiters:
            self._response_waiters.remove(waiter)
        if not waiter.done():
            waiter.cancel()

    def fail_response_waiters(self, error: Exception):
        while self._response_waiters:
            waiter = self._response_waiters.pop(0)
            if not waiter.done():
                waiter.set_exception(error)

    @property
    def text(self) -> str:
        text_response = "".join(self._text_chunks).strip()
        transcript_response = self._merge_transcript_deltas(self._transcript_chunks).strip()
        return max(
            (text_response, transcript_response),
            key=lambda response: len(response),
            default="",
        )

    @staticmethod
    def _merge_transcript_deltas(deltas: list[str]) -> str:
        merged = ""
        for delta in deltas:
            delta = delta.strip()
            if not delta:
                continue
            if delta in merged:
                continue
            if merged and merged in delta:
                merged = delta
                continue
            merged = f"{merged} {delta}".strip()
        return merged


class ActionOut:
    def action_done(self, value: str):
        print(f"[action] {value}")
