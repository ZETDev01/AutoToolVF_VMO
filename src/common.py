import pyaudio

PCM16 = pyaudio.paInt16
CHUNK_SIZE = 1024
RATE_IN = 16000
RATE_OUT = 16000
pya = pyaudio.PyAudio()  # to terminate before exit
