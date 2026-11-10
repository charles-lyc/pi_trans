# PiTrans

**English** | [简体中文](README.zh-CN.md)

Real-time speech translation on a Raspberry Pi. Plug in a USB microphone and speak Chinese or English. The transcript and its translation scroll live on a web page that anyone on the same network can open on a phone or laptop.

## Use cases

- **Tech talks and conferences** (e.g. KiCon Asia): the speaker talks in Chinese and the audience reads English captions on their phones, or the other way around.
- **Meetups, lectures and training**: bilingual captions on a projector or big screen.
- **Cross-language conversations**: a text record of what both sides said.

## Features

- **Two modes**: on-device speech recognition plus cloud translation (low cost, and recognition keeps working offline), or cloud simultaneous interpretation that streams the translation while you speak.
- **Flexible translation**: Chinese → English, English → Chinese, or auto-detect. Six engines are available, with automatic fallback when one fails.
- **Domain glossary**: 211 terms covering hardware, KiCad / PCB, embedded systems, networking and AI.
- **Prompt-injection safe**: speech that sounds like an instruction is translated, never executed.
- **Nothing to install for viewers**: open the page in any browser. Settings stay in sync across devices, and the page shows a live waveform, a status light and per-engine stats.
- **Works like an appliance**: starts on boot, restarts after crashes and reconnects the microphone and cloud automatically. Wi-Fi networks for different venues can be stored in advance.

## How it works

```
USB mic ─► arecord (16 kHz mono)
            ├─ Mode A: Silero VAD ─► SenseVoice (on the Pi) ─► cloud translation engine
            └─ Mode B: audio stream ─► Qwen LiveTranslate (recognition + translation)
                                   │
                    FastAPI + WebSocket ─► browsers (many at once)
```

| Part | Choice |
| --- | --- |
| Hardware | Raspberry Pi 4B (2 GB) + USB microphone |
| Speech recognition | sherpa-onnx with SenseVoice-Small (int8) and Silero VAD, running on the Pi's CPU |
| Translation | Qwen LiveTranslate, Qwen-MT, DeepSeek, GLM-4-Flash, Google, MyMemory |
| Server | Python, FastAPI, WebSocket |
| Web UI | A single HTML file with no frameworks |
| System | systemd services, NetworkManager |

On the Pi the service uses about 510 MB of RAM and about 5% of one core while idle. Recognition runs at about 0.3× real time, and each translation appears 1–2 seconds after the sentence ends.

## Roadmap

- Fix speech recognition errors before showing captions (e.g. "Key CAD" → KiCad)
- True auto-detect direction in cloud interpretation mode
- Per-client send queues so 100+ viewers don't slow each other down
- Field tests in noisy venues and long sessions

## Documentation

The detailed docs are currently in Chinese only.

- [Design](docs/DESIGN.md): architecture, model selection and the full roadmap
- [Configuration & operations](docs/OPERATIONS.md): config reference, glossary, Wi-Fi setup and deploying from scratch
