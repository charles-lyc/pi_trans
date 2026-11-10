"""Speech recognition: arecord -> sherpa-onnx -> callbacks.

Local backends:
- "zipformer": streaming transducer, text appears while speaking, no punctuation.
- "sensevoice": Silero VAD cuts speech into sentences, SenseVoice recognizes each
  one. More accurate and punctuated, but text appears only after a pause.

Cloud backend (selected as the "qwen-live" translation engine):
- QwenLiveRecognizer streams the mic to Qwen LiveTranslate, which returns the
  transcript and its translation together, so on_final carries both.
"""
import base64
import json
import logging
import os
import queue
import re
import subprocess
import threading
import time
from collections import OrderedDict

import numpy as np
import sherpa_onnx
from websockets.sync.client import connect

log = logging.getLogger("asr")

SAMPLE_RATE = 16000
CHUNK_BYTES = SAMPLE_RATE // 10 * 2  # 100 ms of S16_LE mono
VAD_WINDOW = 512  # samples per Silero VAD step
WAVE_BARS = 8  # waveform peaks sent to the UI per chunk
LISTENING_HINT = "🎤"


def _find(model_dir, prefix):
    names = sorted(os.listdir(model_dir))
    # Prefer int8 models: much faster on the Pi with little accuracy loss.
    for want_int8 in (True, False):
        for n in names:
            if n.startswith(prefix) and n.endswith(".onnx") and ("int8" in n) == want_int8:
                return os.path.join(model_dir, n)
    raise FileNotFoundError(f"{prefix}*.onnx not found in {model_dir}")


class Recognizer:
    """Owns the arecord process; subclasses turn audio chunks into text."""

    def __init__(self, device, gain, on_partial, on_final):
        self.device = device
        self.gain = gain
        self.on_partial = on_partial
        self.on_final = on_final
        self.on_level = None  # set by app.py: gets WAVE_BARS peaks (0..1) per chunk
        self.paused = False
        self.running = True
        # Only one recognizer may hold the mic; app.py flips this when switching.
        self.active = True
        self.status = "starting"
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()

    def _run(self):
        while self.running:
            if not self.active:
                time.sleep(0.2)
                continue
            try:
                self._capture()
            except Exception:
                log.exception("capture failed")
            if self.active:
                self.status = "no-mic"
                time.sleep(2)  # mic unplugged or not ready yet at boot; retry

    def _resolve_device(self):
        """'auto' means the first capture card: the Pi has none built in, so it's the USB mic."""
        if self.device != "auto":
            return self.device
        out = subprocess.run(["arecord", "-l"], capture_output=True, text=True).stdout
        m = re.search(r"^card \d+: (\w+) \[(.*?)\]", out, re.M)
        if not m:
            return None
        log.info("auto-selected mic: %s", m.group(2))
        return f"plughw:CARD={m.group(1)},DEV=0"

    def _capture(self):
        device = self._resolve_device()
        if not device:
            log.warning("no capture device found")
            return
        cmd = ["arecord", "-q", "-D", device, "-f", "S16_LE",
               "-r", str(SAMPLE_RATE), "-c", "1", "-t", "raw"]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._reset()
        log.info("listening on %s", device)
        idle = False
        try:
            while self.running and self.active:
                data = proc.stdout.read(CHUNK_BYTES)
                if not data:
                    log.warning("arecord ended: %s", proc.stderr.read().decode(errors="ignore").strip())
                    return
                self.status = "paused" if self.paused else self._listening_status()
                if self.paused:
                    if not idle:
                        self._pause()
                    idle = True
                    continue
                idle = False
                samples = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
                if self.gain != 1.0:
                    samples = np.clip(samples * self.gain, -1.0, 1.0)
                if self.on_level and len(samples) % WAVE_BARS == 0:
                    peaks = np.abs(samples).reshape(WAVE_BARS, -1).max(axis=1)
                    self.on_level([round(float(p), 3) for p in peaks])
                self._process(samples)
        finally:
            proc.kill()
            proc.wait()
            self._close()

    def _listening_status(self):
        return "listening"

    def _reset(self):
        raise NotImplementedError

    def _process(self, samples):
        raise NotImplementedError

    def _pause(self):
        pass

    def _close(self):
        pass


class ZipformerRecognizer(Recognizer):
    def __init__(self, model_dir, num_threads, **kw):
        super().__init__(**kw)
        self.recognizer = sherpa_onnx.OnlineRecognizer.from_transducer(
            tokens=os.path.join(model_dir, "tokens.txt"),
            encoder=_find(model_dir, "encoder"),
            decoder=_find(model_dir, "decoder"),
            joiner=_find(model_dir, "joiner"),
            num_threads=num_threads,
            sample_rate=SAMPLE_RATE,
            feature_dim=80,
            decoding_method="greedy_search",
            enable_endpoint_detection=True,
            rule1_min_trailing_silence=2.4,
            rule2_min_trailing_silence=0.8,
            rule3_min_utterance_length=20,
        )

    def _reset(self):
        self.stream = self.recognizer.create_stream()
        self.last_partial = ""

    def _process(self, samples):
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        while self.recognizer.is_ready(self.stream):
            self.recognizer.decode_stream(self.stream)
        # The bilingual model emits English in upper case.
        text = self.recognizer.get_result(self.stream).strip().lower()
        if self.recognizer.is_endpoint(self.stream):
            if text:
                self.on_final(text)
            elif self.last_partial:
                self.on_partial("")
            self.last_partial = ""
            self.recognizer.reset(self.stream)
        elif text != self.last_partial:
            self.last_partial = text
            self.on_partial(text)


class SenseVoiceRecognizer(Recognizer):
    def __init__(self, model_dir, vad_model, vad_params, num_threads, language, **kw):
        super().__init__(**kw)
        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=_find(model_dir, "model"),
            tokens=os.path.join(model_dir, "tokens.txt"),
            num_threads=num_threads,
            # "auto" misreads short Mandarin fragments as Japanese; "zh" still
            # transcribes English words fine.
            language=language,
            use_itn=True,
        )
        cfg = sherpa_onnx.VadModelConfig()
        cfg.silero_vad.model = vad_model
        cfg.silero_vad.threshold = vad_params.get("threshold", 0.5)
        cfg.silero_vad.min_silence_duration = vad_params.get("min_silence_duration", 0.7)
        cfg.silero_vad.min_speech_duration = vad_params.get("min_speech_duration", 0.5)
        cfg.silero_vad.max_speech_duration = vad_params.get("max_speech_duration", 10)
        cfg.sample_rate = SAMPLE_RATE
        log.info("vad: threshold=%.2f silence=%.2fs speech=%.2f-%.0fs",
                 cfg.silero_vad.threshold, cfg.silero_vad.min_silence_duration,
                 cfg.silero_vad.min_speech_duration, cfg.silero_vad.max_speech_duration)
        self.vad_config = cfg
        # Decoding a sentence takes ~0.3x its length; do it off the capture
        # thread so arecord's pipe never backs up.
        self.segments = queue.Queue()
        threading.Thread(target=self._decode_loop, daemon=True).start()

    def _reset(self):
        self.vad = sherpa_onnx.VoiceActivityDetector(self.vad_config, buffer_size_in_seconds=60)
        self.pending = np.zeros(0, dtype=np.float32)
        self.speaking = False

    def _process(self, samples):
        self.pending = np.concatenate([self.pending, samples])
        while len(self.pending) >= VAD_WINDOW:
            self.vad.accept_waveform(self.pending[:VAD_WINDOW])
            self.pending = self.pending[VAD_WINDOW:]
        speaking = self.vad.is_speech_detected()
        if speaking and not self.speaking:
            self.on_partial(LISTENING_HINT)
        self.speaking = speaking
        while not self.vad.empty():
            self.segments.put(np.array(self.vad.front.samples, dtype=np.float32))
            self.vad.pop()

    def _decode_loop(self):
        while True:
            samples = self.segments.get()
            try:
                stream = self.recognizer.create_stream()
                stream.accept_waveform(SAMPLE_RATE, samples)
                self.recognizer.decode_stream(stream)
                text = stream.result.text.strip()
            except Exception:
                log.exception("decode failed")
                text = ""
            if text and any(c.isalnum() for c in text):
                self.on_final(text)
            elif not self.speaking:
                self.on_partial("")


class QwenLiveRecognizer(Recognizer):
    """Streams 16 kHz PCM to Qwen LiveTranslate over its realtime WebSocket.

    The server segments speech itself. Per utterance it sends the transcript as
    conversation.item.input_audio_transcription.* events and the translation as
    response.text.* events on a separate output item. Deltas are increments,
    appended in arrival order; the 3.5-style *.text events instead carry the
    confirmed text plus a provisional "stash" that replaces what came before.
    """
    RETRY_SECONDS = 5
    FINAL_WAIT = 1.5  # translation done but transcript not yet: wait this long
    FLUSH_SECONDS = 4  # silence sent before session.finish so the last utterance ends

    def __init__(self, engine, glossary, **kw):
        super().__init__(**kw)
        self.engine = engine  # translators.QwenLive: config and token counters
        self.glossary = glossary
        self.src, self.dst = "zh", "en"
        self.ws = None
        self.failed = False
        self.retry_at = 0
        self.lock = threading.RLock()
        self.items = OrderedDict()  # transcript item id -> utterance state
        self.source_of = {}  # translation item id -> transcript item id
        self.last_source = None

    def set_direction(self, src, dst):
        if (src, dst) != (self.src, self.dst):
            self.src, self.dst = src, dst
            self._close()  # the target language is fixed per session; reconnect

    def _listening_status(self):
        if self.ws:
            return "listening"
        return "cloud-error" if self.failed else "connecting"

    def _reset(self):
        self._close()

    def _pause(self):
        self._close()  # flush the current utterance; resuming reconnects

    def _process(self, samples):
        if self.ws is None:
            if time.time() < self.retry_at:
                return
            try:
                self._connect()
                self.failed = False
            except Exception as e:
                log.warning("qwen-live connect failed: %r", e)
                self.failed = True
                self.retry_at = time.time() + self.RETRY_SECONDS
                return
        pcm = (samples * 32767).astype(np.int16).tobytes()
        try:
            self.ws.send(json.dumps({"type": "input_audio_buffer.append",
                                     "audio": base64.b64encode(pcm).decode("ascii")}))
        except Exception as e:
            log.warning("qwen-live send failed: %r", e)
            self.ws = None

    def _connect(self):
        cfg = self.engine.cfg
        ws = connect(f"{cfg['url']}?model={cfg['model']}",
                     additional_headers={"Authorization": f"Bearer {cfg['api_key']}"},
                     proxy=self.engine.proxy, open_timeout=10)
        try:
            self._expect(ws, "session.created")
            translation = {"language": self.dst}
            phrases = {g[self.src]: g[self.dst] for g in self.glossary
                       if g.get(self.src) and g.get(self.dst)}
            if phrases:
                translation["corpus"] = {"phrases": phrases}
            session = {"output_modalities": ["text"], "translation": translation}
            # The default speaker_detection only cuts on long pauses (a 30 s
            # monologue came back as one sentence); server_vad cuts at each pause.
            silence = cfg.get("silence_ms", 600)
            if silence:
                session["audio"] = {"input": {"turn_detection": {
                    "type": "server_vad", "threshold": cfg.get("vad_threshold", 0.5),
                    "silence_duration_ms": silence}}}
            ws.send(json.dumps({"type": "session.update", "session": session}))
            self._expect(ws, "session.updated")
        except Exception:
            ws.close()
            raise
        log.info("qwen-live session open: %s -> %s", self.src, self.dst)
        self.ws = ws
        threading.Thread(target=self._receive, args=(ws,), daemon=True).start()

    @staticmethod
    def _expect(ws, want):
        while True:
            event = json.loads(ws.recv(timeout=10))
            if event.get("type") == "error":
                raise RuntimeError(event.get("error"))
            if event.get("type") == want:
                return event

    def _close(self):
        ws, self.ws = self.ws, None
        if not ws:
            return
        # Trailing silence lets the VAD close the last utterance, session.finish
        # flushes it; the receiver keeps handling its results until the server
        # closes, or we do after a grace period.
        silence = base64.b64encode(bytes(CHUNK_BYTES)).decode("ascii")
        try:
            for _ in range(self.FLUSH_SECONDS * 10):
                ws.send(json.dumps({"type": "input_audio_buffer.append", "audio": silence}))
            ws.send(json.dumps({"type": "session.finish"}))
        except Exception:
            pass
        threading.Timer(5, ws.close).start()

    def _receive(self, ws):
        usage_seen = [0, 0]  # response.done usage is cumulative per session
        try:
            for raw in ws:
                event = json.loads(raw)
                if event.get("type") == "session.finished":
                    break
                with self.lock:
                    self._handle(event, usage_seen)
        except Exception as e:
            log.warning("qwen-live connection lost: %r", e)
        finally:
            ws.close()
            if self.ws is ws:
                self.ws = None

    def _item(self, item_id):
        if item_id not in self.items:
            self.items[item_id] = dict(text="", translation="", heard=False,
                                       translated=False, stopped=None, final=False)
            while len(self.items) > 50:
                self.items.popitem(last=False)
        return self.items[item_id]

    def _source(self, translation_id):
        src_id = self.source_of.get(translation_id) or self.last_source
        return self._item(src_id) if src_id else None

    def _handle(self, event, usage_seen):
        kind = event.get("type", "")
        if kind == "error":
            log.warning("qwen-live error: %s", event.get("error"))
        elif kind == "input_audio_buffer.speech_started":
            self.last_source = event["item_id"]
            self._show(self._item(event["item_id"]))
        elif kind == "input_audio_buffer.speech_stopped":
            self._item(event["item_id"])["stopped"] = time.time()
        elif kind == "conversation.item.created":
            # Documented link: translation item -> transcript item.
            item = event.get("item") or {}
            if item.get("role") == "assistant" and event.get("previous_item_id"):
                self.source_of[item["id"]] = event["previous_item_id"]
        elif kind == "response.output_item.added":
            # Seen in practice instead: the translation item is announced here
            # right after speech_started, with no previous_item_id anywhere.
            item_id = (event.get("item") or {}).get("id")
            if item_id and item_id not in self.source_of and self.last_source:
                self.source_of[item_id] = self.last_source
        elif kind == "conversation.item.input_audio_transcription.delta":
            self.last_source = event["item_id"]
            it = self._item(event["item_id"])
            it["text"] += event.get("delta", "")
            self._show(it)
        elif kind == "conversation.item.input_audio_transcription.text":
            self.last_source = event["item_id"]
            it = self._item(event["item_id"])
            it["text"] = event.get("text", "") + event.get("stash", "")
            self._show(it)
        elif kind in ("response.text.text", "response.audio_transcript.text"):
            it = self._source(event.get("item_id"))
            if it:
                it["translation"] = event.get("text", "") + event.get("stash", "")
                self._show(it)
        elif kind == "conversation.item.input_audio_transcription.completed":
            it = self._item(event["item_id"])
            it["text"] = event.get("transcript") or it["text"]
            it["heard"] = True
            self._finish(it)
        elif kind in ("response.text.delta", "response.audio_transcript.delta"):
            it = self._source(event.get("item_id"))
            if it:
                it["translation"] += event.get("delta", "")
                self._show(it)
        elif kind in ("response.text.done", "response.audio_transcript.done"):
            it = self._source(event.get("item_id"))
            if it:
                it["translation"] = event.get("text") or event.get("transcript") or it["translation"]
                self._translated(it)
        elif kind == "response.done":
            response = event.get("response") or {}
            usage = response.get("usage") or {}
            tokens = [usage.get("input_tokens", 0), usage.get("output_tokens", 0)]
            self.engine.tokens_in += max(0, tokens[0] - usage_seen[0])
            self.engine.tokens_out += max(0, tokens[1] - usage_seen[1])
            usage_seen[:] = tokens
            for out in response.get("output") or []:
                it = self._source(out.get("id"))
                if it:
                    self._translated(it)

    def _show(self, it):
        if not it["final"]:
            self.on_partial(it["text"] or LISTENING_HINT, it["translation"])

    def _translated(self, it):
        if it["translated"]:
            return
        it["translated"] = True
        if it["heard"]:
            self._finish(it)
        else:
            threading.Timer(self.FINAL_WAIT, self._force_finish, args=(it,)).start()

    def _force_finish(self, it):
        with self.lock:
            it["heard"] = True
            self._finish(it)

    def _finish(self, it):
        if it["final"] or not (it["heard"] and it["translated"]):
            return
        it["final"] = True
        text, translation = it["text"].strip(), it["translation"].strip()
        if not text and not translation:
            self.on_partial("")
            return
        elapsed = time.time() - it["stopped"] if it["stopped"] else None
        self.on_final(text or "…", dict(translation=translation, src=self.src, dst=self.dst,
                                       engine=self.engine.name, elapsed=elapsed))


def create_live(config, engine, on_partial, on_final):
    return QwenLiveRecognizer(engine, config.get("glossary", []), device=config["audio_device"],
                              gain=config.get("input_gain", 1.0),
                              on_partial=on_partial, on_final=on_final)


def create(config, on_partial, on_final):
    kind = config.get("asr", "sensevoice")
    models = config["models"]
    common = dict(device=config["audio_device"], gain=config.get("input_gain", 1.0),
                  on_partial=on_partial, on_final=on_final)
    if kind == "zipformer":
        return ZipformerRecognizer(models["zipformer"], config.get("num_threads", 4), **common)
    return SenseVoiceRecognizer(models["sensevoice"], models["vad"], config.get("vad", {}),
                                config.get("num_threads", 4),
                                config.get("asr_language", "zh"), **common)
