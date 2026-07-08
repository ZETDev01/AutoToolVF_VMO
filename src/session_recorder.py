import wave
from datetime import datetime
from pathlib import Path

RATE_IN = 16000
RATE_OUT = 16000


class SessionRecorder:
    def __init__(self, base_dir: str = "runs"):
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.session_dir = Path(base_dir) / timestamp
        self.session_dir.mkdir(parents=True, exist_ok=True)

        self.audio_in_path = self.session_dir / "audio_in.wav"
        self.audio_out_path = self.session_dir / "audio_out.wav"

        self._audio_in = self._open_wave(self.audio_in_path, RATE_IN)
        self._audio_out = self._open_wave(self.audio_out_path, RATE_OUT)

    @staticmethod
    def _open_wave(path: Path, sample_rate: int):
        stream = wave.open(str(path), "wb")
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(sample_rate)
        return stream

    def write_audio_in(self, chunk: bytes):
        self._audio_in.writeframes(chunk)

    def write_audio_out(self, chunk: bytes):
        self._audio_out.writeframes(chunk)

    def close(self):
        self._audio_in.close()
        self._audio_out.close()
