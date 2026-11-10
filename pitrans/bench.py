"""Compare zipformer (streaming) vs SenseVoice (offline) on the same wav files."""
import sys
import time
import wave

import numpy as np
import sherpa_onnx

from asr import _find

ZF = "models/sherpa-onnx-streaming-zipformer-bilingual-zh-en-2023-02-20"
SV = "models/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17"


def load(path):
    w = wave.open(path)
    a = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
    if w.getnchannels() > 1:
        a = a.reshape(-1, w.getnchannels()).mean(axis=1)
    return a, w.getframerate()


t0 = time.time()
zf = sherpa_onnx.OnlineRecognizer.from_transducer(
    tokens=ZF + "/tokens.txt", encoder=_find(ZF, "encoder"), decoder=_find(ZF, "decoder"),
    joiner=_find(ZF, "joiner"), num_threads=4, sample_rate=16000, feature_dim=80)
print(f"zipformer load {time.time() - t0:.1f}s")
t0 = time.time()
sv = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=SV + "/model.int8.onnx", tokens=SV + "/tokens.txt", num_threads=4,
    use_itn=True, language="auto")
print(f"sensevoice load {time.time() - t0:.1f}s")

for path in sys.argv[1:]:
    a, sr = load(path)
    dur = len(a) / sr
    print(f"\n== {path} ({dur:.1f}s)")

    s = zf.create_stream()
    t0 = time.time()
    s.accept_waveform(sr, a)
    s.accept_waveform(sr, np.zeros(sr // 2, np.float32))
    s.input_finished()
    while zf.is_ready(s):
        zf.decode_stream(s)
    dt = time.time() - t0
    print(f"  zipformer  RTF {dt / dur:.2f}: {zf.get_result(s).lower()}")

    s = sv.create_stream()
    t0 = time.time()
    s.accept_waveform(sr, a)
    sv.decode_stream(s)
    dt = time.time() - t0
    print(f"  sensevoice RTF {dt / dur:.2f}: {s.result.text}")
