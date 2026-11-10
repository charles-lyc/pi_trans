"""PiTrans: live speech translation served as a web page."""
import asyncio
import itertools
import json
import logging
import os
import re
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

import asr
from translators import build_engines

BASE = Path(__file__).resolve().parent
CONFIG_PATH = BASE / "config.json"
DIRECTIONS = {"zh2en": ("zh", "en"), "en2zh": ("en", "zh"), "auto": None}
CJK = re.compile(r"[一-鿿]")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("app")

config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
engines = build_engines(config)
history = deque(maxlen=300)
clients = set()
ids = itertools.count(1)
state = {"partial": "", "partial_translation": ""}
# Per-engine counters since service start; token totals live on the LLM engines.
stats = {name: {"ok": 0, "fail": 0, "seconds": 0.0, "last_error": ""} for name in engines}
started = time.time()
recognizer = None  # the one holding the mic
recognizers = {}  # "local" / "live", built on first use and kept for quick switching
loop = None


def save_config():
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def public_config():
    return {
        "direction": config["direction"],
        "engine": config["engine"],
        "paused": recognizer.paused if recognizer else False,
        "engines": [{"name": e.name, "label": e.label, "available": e.available(),
                     "live": getattr(e, "live", False)}
                    for e in engines.values()],
    }


def public_stats():
    rows = []
    for name, e in engines.items():
        s = stats[name]
        rows.append({
            "label": e.label, "available": e.available(), "ok": s["ok"], "fail": s["fail"],
            "avg": round(s["seconds"] / s["ok"], 2) if s["ok"] else None,
            "tokens_in": getattr(e, "tokens_in", None), "tokens_out": getattr(e, "tokens_out", None),
            "last_error": s["last_error"],
        })
    return {"since": started, "engines": rows}


async def broadcast(msg):
    data = json.dumps(msg, ensure_ascii=False)
    for ws in list(clients):
        try:
            await ws.send_text(data)
        except Exception:
            clients.discard(ws)


def resolve_direction(text):
    pair = DIRECTIONS.get(config["direction"])
    if pair:
        return pair
    return ("zh", "en") if CJK.search(text) else ("en", "zh")


def recent_context(entry):
    """Last few translated sentences in the same direction, oldest first."""
    pairs = []
    for prev in reversed(history):
        if len(pairs) >= config.get("context_sentences", 3):
            break
        if prev["id"] < entry["id"] and prev.get("translation") and prev["src"] == entry["src"]:
            pairs.append((prev["text"], prev["translation"]))
    return pairs[::-1]


async def translate(entry):
    first = config["engine"]
    order = [first] + ([n for n in engines if n != first] if config.get("fallback") else [])
    context = recent_context(entry)
    errors = []
    for name in order:
        engine = engines[name]
        if not engine.available() or getattr(engine, "live", False):
            continue
        try:
            t0 = time.time()
            entry["translation"] = await engine.translate(
                entry["text"], entry["src"], entry["dst"], context)
            elapsed = time.time() - t0
            log.info("%s %.2fs: %s => %s", name, elapsed, entry["text"], entry["translation"])
            entry["engine"] = engine.label
            stats[name]["ok"] += 1
            stats[name]["seconds"] += elapsed
            break
        except Exception as e:
            log.warning("%s failed: %r", name, e)
            errors.append(f"{name}: {e!r}"[:120])
            stats[name]["fail"] += 1
            stats[name]["last_error"] = f"{time.strftime('%H:%M:%S')} {e!r}"[:160]
    else:
        entry["error"] = "; ".join(errors) or "no engine available"
    await broadcast({"type": "update", "entry": entry})
    await broadcast({"type": "stats", "stats": public_stats()})


async def handle_final(text, live=None):
    """`live` is set when the recognizer translated the speech itself (Qwen 同传)."""
    src, dst = (live["src"], live["dst"]) if live else resolve_direction(text)
    entry = {"id": next(ids), "text": text, "src": src, "dst": dst, "ts": time.time(),
             "translation": None, "engine": None}
    history.append(entry)
    state["partial"] = state["partial_translation"] = ""
    if not live:
        await broadcast({"type": "final", "entry": entry})
        asyncio.create_task(translate(entry))
        return
    name = live["engine"]
    if live["translation"]:
        entry["translation"] = live["translation"]
        entry["engine"] = engines[name].label
        stats[name]["ok"] += 1
        stats[name]["seconds"] += live["elapsed"] or 0
        log.info("%s: %s => %s", name, text, entry["translation"])
    else:
        entry["error"] = "no translation returned"
        stats[name]["fail"] += 1
    await broadcast({"type": "final", "entry": entry})
    await broadcast({"type": "stats", "stats": public_stats()})


async def handle_partial(text, translation=""):
    state["partial"], state["partial_translation"] = text, translation
    await broadcast({"type": "partial", "text": text, "translation": translation})


def live_engine():
    """The selected engine if it is a working speech-in engine, else None."""
    engine = engines.get(config["engine"])
    return engine if getattr(engine, "live", False) and engine.available() else None


def live_direction():
    # Qwen 同传 detects the source language itself but needs a fixed target;
    # "auto" falls back to zh -> en.
    return DIRECTIONS.get(config["direction"]) or ("zh", "en")


def build_recognizer(kind):
    callbacks = dict(
        on_partial=lambda *a: asyncio.run_coroutine_threadsafe(handle_partial(*a), loop),
        on_final=lambda *a: asyncio.run_coroutine_threadsafe(handle_final(*a), loop),
    )
    if kind == "live":
        r = asr.create_live(config, live_engine(), **callbacks)
    else:
        asr_config = dict(config, models={k: str(BASE / v) for k, v in config["models"].items()})
        r = asr.create(asr_config, **callbacks)
    r.on_level = lambda peaks: asyncio.run_coroutine_threadsafe(
        broadcast({"type": "level", "peaks": peaks}), loop)
    return r


async def use_recognizer():
    """Give the mic to the recognizer the selected engine needs."""
    global recognizer
    kind = "live" if live_engine() else "local"
    new = recognizers.get(kind)
    if new is None:
        new = recognizers[kind] = await asyncio.to_thread(build_recognizer, kind)
    if kind == "live":
        new.engine = live_engine()
        new.set_direction(*live_direction())
    if new is recognizer:
        return
    if recognizer:
        new.paused = recognizer.paused
        recognizer.active = False
    new.active = True
    if not new.thread.is_alive():
        new.start()
    log.info("recognizer: %s", kind)
    recognizer = new


async def status_watcher():
    last = None
    while True:
        if recognizer.status != last:
            last = recognizer.status
            await broadcast({"type": "status", "status": last})
        await asyncio.sleep(0.5)


@asynccontextmanager
async def lifespan(_app):
    global loop
    loop = asyncio.get_running_loop()
    await use_recognizer()
    watcher = asyncio.create_task(status_watcher())
    yield
    for r in recognizers.values():
        r.running = False
    watcher.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/")
async def index():
    return FileResponse(BASE / "static" / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    clients.add(ws)
    await ws.send_text(json.dumps({
        "type": "init", "config": public_config(), "history": list(history),
        "partial": state["partial"], "partial_translation": state["partial_translation"],
        "status": recognizer.status, "stats": public_stats(),
    }, ensure_ascii=False))
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            if msg.get("type") == "set":
                if msg.get("direction") in DIRECTIONS:
                    config["direction"] = msg["direction"]
                if msg.get("engine") in engines:
                    config["engine"] = msg["engine"]
                if "paused" in msg:
                    recognizer.paused = bool(msg["paused"])
                await use_recognizer()
                save_config()
                await broadcast({"type": "config", "config": public_config()})
            elif msg.get("type") == "clear":
                history.clear()
                await broadcast({"type": "clear"})
    except WebSocketDisconnect:
        pass
    finally:
        clients.discard(ws)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
