# 🪨 Rocky — a fully local, private AI assistant

![Local](https://img.shields.io/badge/AI-100%25%20local-success)
![Privacy](https://img.shields.io/badge/data-never%20leaves%20your%20network-brightgreen)
![Python](https://img.shields.io/badge/Python-3.11-blue?logo=python&logoColor=white)
![Ollama](https://img.shields.io/badge/LLM-Ollama-black)
![Whisper](https://img.shields.io/badge/STT-whisper.cpp-5a4fcf)
![XTTS](https://img.shields.io/badge/TTS-Coqui%20XTTS--v2-ff6f61)
![Tailscale](https://img.shields.io/badge/network-Tailscale-1d3c6e?logo=tailscale&logoColor=white)

> A voice assistant **and** a coding setup that run entirely on a home GPU — no OpenAI, no Anthropic, no cloud. You talk to **Rocky** (voiced after the Eridian from *Project Hail Mary*) from a laptop; everything is computed on a always-on PC and reached over an encrypted [Tailscale](https://tailscale.com) tunnel. Built to find out **how far local models can actually go** for day-to-day work.

---

## ✨ Highlights

- 🎙️ **Talk to it.** Wake-word → speech-to-text → local LLM → a **cloned voice** answers — round-trip in a few seconds.
- 🔒 **Genuinely private.** Mic audio, transcripts, replies, and your Jira data never leave your Tailnet. No third-party AI APIs anywhere in the path.
- 🧠 **Reads your Jira.** Ask "what do I need to do for PROJ-123?" and it pulls the real ticket — even when speech mangles the key into "proj 123".
- ⚡ **Tuned for latency.** Streaming reply pipeline, model-stays-warm, VAD recording, and a 10× token cut from one well-placed API flag (see [Engineering notes](#-engineering-notes)).
- 💻 **Local coding too.** The same GPU backs an IDE coding assistant (Ollama + the [Continue](https://continue.dev) extension), calling the models directly — still nothing-leaves-the-network.
- 📊 **Observable.** Built-in status dashboard, rotating logs, conversation memory across restarts.
- 🛡️ **Hardened.** Optional shared-secret auth, XSS-safe dashboard, injection-safe TTS, regex-bounded Jira queries.

---

## 🏗️ Architecture

```
  MacBook (M3)  ── client ──┐                        ┌── PC (Ryzen 9 + RTX 5080, always on)
                            │                        │
  🎙️ ffmpeg  record mic    │                        │   🧠 rocky_server.py  (0.0.0.0:7437)
  📝 whisper  speech→text   │   ╔══════════════╗     │      • Jira REST (reads your tickets)
  🌐 HTTP POST  ───────────►│   ║  Tailscale   ║────►│      • Ollama chat (streaming)
  🔊 rocky_say  speak reply │   ║  (encrypted) ║     │      • memory · logging · dashboard
                            │   ╚══════════════╝     │
  💻 Cursor + Continue ─────┘                        │   🤖 Ollama (Windows service, :11434)
     → Ollama over Tailscale                         │      • qwen3:8b          (voice)
     → Atlassian Rovo MCP (Jira)                     │      • qwen2.5-coder:7b   (coding)
                                                     │      • qwen2.5:7b-instruct(tools)
```

Two independent surfaces, one local brain:
1. **Rocky** — the voice assistant (speak to it, it answers and reads Jira).
2. **Coding** — your IDE using the same local models for chat/edit.

---

## 🧩 What's inside

| Component | What it does | Tech |
|---|---|---|
| 🎙️ **Voice client** (`rocky_client.py`) | Wake word, VAD recording, STT, streamed spoken replies | ffmpeg · whisper.cpp · afplay |
| 🗣️ **Voice (rocky_say)** | Rocky's **cloned voice** from a short reference clip | Coqui **XTTS-v2** |
| 🧠 **Orchestration server** (`rocky_server.py`) | Routes queries, adds Jira context, streams from the LLM | Python stdlib + PyYAML |
| 🤖 **Local models** | Voice + coding inference on a 16 GB GPU | **Ollama** (qwen3 / qwen2.5) |
| 📋 **Jira integration** | Lists your tickets, reads descriptions, voice-tolerant key matching | Jira Cloud REST v3 |
| 💾 **Memory** | Rolling conversation history, persisted across restarts | JSONL |
| 📊 **Dashboard** | Live status: model, VRAM, Jira, recent queries | zero-dep HTML at `GET /` |

---

## 🛠️ Tech stack

**Models** qwen3:8b (voice) · qwen2.5-coder:7b + qwen2.5:7b-instruct (coding) — all chosen to fit **100 % in 16 GB VRAM**
**Inference** Ollama · **STT** whisper.cpp (small.en) · **TTS** Coqui XTTS-v2 (voice-cloned)
**Transport** Tailscale (WireGuard) · **Server** Python 3.11 standard library (+ PyYAML)
**IDE** Cursor + Continue → local Ollama · Atlassian Rovo MCP
**Hardware** AMD Ryzen 9 9950X3D · 96 GB RAM · RTX 5080 (16 GB) · Apple M3

---

## 🧠 Engineering notes

The fun part — real problems hit and solved while building this:

- 🏎️ **The 10× win hiding in plain sight.** qwen3 *ignores* the `/no_think` prompt token in Ollama and kept emitting hundreds of chain-of-thought tokens per reply. Switching to the explicit **`think: false`** API param cut a "say hello" from **246 tokens → 22**. Single biggest latency fix.
- 🎛️ **Right-sizing for the GPU.** A 30B model spilled ~26 % to CPU and took **15–19 s** to cold-reload after the coding model evicted it. Moving the voice model to one that fits **100 % in VRAM** made replies fast *and consistent* (`ollama ps` to watch the split).
- 🗣️ **Streaming TTS pipeline.** Reply is streamed token-by-token, split into sentences, and synthesized **while the previous sentence is still playing** — first audio in ~1–2 s instead of waiting for the whole answer.
- ✂️ **Recording that stops when you do.** The original "record 10 s" was replaced with an energy-based VAD that ends the take ~1 s after you stop talking.
- 🐛 **Voice-tolerant ticket matching.** STT turns `PROJ-123` into "PROJ 123"; a fuzzy matcher resolves it against the instance's real project keys (paginated — the key was on page 2!).
- 🔌 **A networking ghost.** Ollama looked dead (connection reset on `:11434`) — a stale `netsh portproxy` rule was hijacking IPv4 and forwarding to a long-gone WSL VM. Classic.
- 🧪 **Knowing when to stop.** In-editor agentic Jira via Continue + Ollama hits an [unresolved Continue bug](https://github.com/continuedev/continue/issues/9529) (streamed tool-calls get fragmented). Rather than fight it, Jira lives in the voice assistant (where it works) and the IDE does chat/edit.

---

## 🚀 Quick start

> Real values (IPs, email, Jira domain) live in local, git-ignored `config.yaml` / `.env`. The repo ships only `*.example` files.

```bash
git clone https://github.com/MartinSWDev/rocky_ai && cd rocky_ai
cp config.example.yaml config.yaml      # IPs, models, paths
cp .env.example .env                     # Jira token (server only)
```

**🖥️ PC (server)** — Windows + Ollama:
```powershell
py -m pip install -r requirements-server.txt
# OLLAMA_HOST=0.0.0.0:11434 (system env), then restart Ollama
ollama pull qwen3:8b
py rocky_server.py                       # dashboard at http://<pc-tailscale-ip>:7437/
```

**💻 Mac (client)** — voice:
```bash
python3 -m pip install -r requirements-client.txt
brew install ffmpeg whisper-cpp
curl -L -o ~/.whisper/ggml-small.en.bin \
  https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
python3.11 -m venv ~/.rocky_say/venv && ~/.rocky_say/venv/bin/pip install -r requirements-tts.txt
scripts/mac/start_rocky.sh --voice       # "Hey Rocky"
```

**💻 Coding in the IDE** — see [`docs/continue.config.example.yaml`](docs/continue.config.example.yaml) for the verified Continue config (local Ollama over Tailscale + Atlassian MCP).

Auto-start at login: `scripts/windows/install_task.ps1` (PC) · `scripts/mac/install_launchd.sh --whisper` (Mac).

---

## ⚡ Voice latency

Levers, in impact order (all config-driven):
1. **Stop-on-silence recording** (`voice.silence_rms` / `silence_secs`).
2. **Small, warm Whisper** — `small.en` via a whisper.cpp server (no per-utterance reload).
3. **Streamed reply** (`voice.stream`) — spoken sentence-by-sentence.
4. **VRAM-fit voice model** — no CPU spill, no slow reloads.

Result: spoken round-trip dropped from **~16–18 s → ~3–5 s** to first audio.

---

## 🔒 Security & privacy

- 🚫 **No third-party AI.** Every model runs locally; prompts and data stay on your Tailnet.
- 🧱 **Keep it behind Tailscale.** Don't port-forward `7437`/`11434`; firewall them to the VPN interface.
- 🔑 **Optional shared secret** (`ROCKY_AUTH_TOKEN`) gates every query and the dashboard.
- 🧼 **Safe by construction** — HTML-escaped dashboard (no XSS), file-based (not interpolated) TTS (no code injection), regex-bounded Jira keys (no SSRF), request-size caps.
- 🙈 **Secrets** live only in git-ignored `.env` / `config.yaml`.

---

## ⚙️ Configuration

One `config.yaml` (non-secret) + `.env` (secrets) drive both machines — models, persona, memory, Jira keywords, voice/latency knobs, logging. See [`config.example.yaml`](config.example.yaml) for the fully-commented reference.

---

## 🩹 Troubleshooting

- **Can't reach server** → PC on? Tailscale up? `curl http://<pc-ip>:7437/health`.
- **Slow / inconsistent replies** → model spilling to CPU; use one that fits VRAM (`ollama ps`).
- **Rocky stuck on a wrong answer** → say **"forget"** (clears conversation memory).
- **Ollama "connection reset"** → check for stale `netsh interface portproxy` rules on `:11434`.
- **IDE chat truncates after one word** → you're using a *reasoning* model; switch to a non-reasoning one (qwen2.5).

---

## 🗺️ Roadmap

- [x] Config-driven, auto-start on both machines
- [x] Conversation memory · logging · status dashboard
- [x] Streaming voice pipeline · latency tuning
- [x] Voice-tolerant Jira ticket lookup
- [ ] Comments / subtasks in Jira context
- [ ] Fast on-device wake word (openWakeWord)
- [ ] More MCP integrations (GitHub, Confluence)

---

<sub>🧪 A personal experiment in how far local models can go for real work. Built with Ollama, whisper.cpp, Coqui XTTS, and Tailscale. Not affiliated with Atlassian, Anthropic, or OpenAI.</sub>
