import asyncio
import json
from asyncio.subprocess import PIPE
from time import monotonic
import numpy as np
from typing import Callable
import platform


BYTE_SIZE = 3200

class WuwModel:
    """Inputs:
    RATE = 16000
    FORMAT = pcm16
    CHUNK = 0.1s = 1600 frames = 3200 bytes
    PING_GAP = 2secs: min gap between wakeups
    """

    def __init__(
        self,
        threshold: float = 0.5,
        exe_path: str = "assets/vinmo_optimized_{}.wuw".format(platform.machine()),
    ):

        print(exe_path)
        self.threshold = threshold
        self.exe = exe_path
        self.ping_gap = 2
        self.exp_at = monotonic() + self.ping_gap

    async def load(self):
        print("wuw initializing..")
        self.proc = await asyncio.create_subprocess_exec(
            self.exe, stdin=PIPE, stdout=PIPE, stderr=PIPE
        )
        await self.infer(b"\x00" * 3200)
        print("wuw initialized.")

    async def infer(self, chunk: bytes):
        dat = np.frombuffer(chunk, np.int16).tolist()
        line_in = json.dumps(dat).encode() + b"\n"

        self.proc.stdin.write(line_in)
        await self.proc.stdin.drain()

        line_out = await self.proc.stdout.readline()
        result = json.loads(line_out.strip())
        if "error" in result:
            raise RuntimeError(f"WUW inference error: {result['error']}")
        score = result["output"]
        # print("{:.2f}".format(score))
        if score >= self.threshold and monotonic() >= self.exp_at:
            self.exp_at = monotonic() + self.ping_gap
            return True
        else:
            return False

    async def close(self):
        self.proc.stdin.close()
        print("wuw exit:", await self.proc.wait())



class WuwClient:
    def __init__(self, model: WuwModel):
        self.model = model
        self._queue = asyncio.Queue()
        self.callbacks: list[Callable] = []

    async def send_audio(self, chunk: bytes):
        await self._queue.put(chunk)

    async def run(self):
        buffer = b""
        while True:
            data = await self._queue.get()
            buffer += data
            while len(buffer) >= BYTE_SIZE:
                chunk = buffer[:BYTE_SIZE]
                buffer = buffer[BYTE_SIZE:]
                is_kw = await self.model.infer(chunk)
                if is_kw:
                    print("ting !!")
                    for callback in self.callbacks:
                        await callback()


if __name__ == "__main__":
    import pyaudio
    from struct import unpack
    from src.common import CHUNK_SIZE, RATE_IN, pya
    async def main():
        model = WuwModel()
        await model.load()
        pya = pyaudio.PyAudio()
        stream = pya.open(format=pyaudio.paInt16, channels=1, rate=RATE_IN, input=True)
        while True:
            data = stream.read(1600)

            is_kw = await model.infer(data)
            if is_kw:
                print("ting")

    asyncio.run(main())
