# Rocky 🪨

A fully local, private AI assistant. Speak to **Rocky** (voiced after the Eridian
from *Project Hail Mary*) from a MacBook; he runs on a home PC, answers general
questions, and reads your Jira tickets. The same local models also back a
**Cursor coding agent**. Nothing is sent to third-party AI services — traffic
goes PC↔Mac over a [Tailscale](https://tailscale.com) tunnel.

Two ways to use it:

1. **Rocky voice assistant** — ask anything; general Q&A by default, Jira lookups
   when you mention a ticket or keyword.
2. **General AI / coding agent** — Cursor IDE talks to the same Ollama models
   (OpenAI-compatible API) plus the Atlassian Rovo MCP.

---

## Architecture

```
MacBook (client)                         PC / home-pc (server, always on)
─────────────────                        ──────────────────────────────────
rocky_client.py                          rocky_server.py  (0.0.0.0:7437)
  ffmpeg     record mic                    Jira REST API   fetch tickets
  whisper    speech → text                 Ollama /api/chat  qwen3:30b-a3b
  HTTP POST  ──── Tailscale ────────────►  conversation memory + logging
  rocky_say  speak reply                    dashboard at GET /
                                           Ollama (Windows service, :11434)
Cursor IDE ──── http://PC:11434/v1 ──────►   qwen3:30b-a3b   (voice)
           ──── Atlassian Rovo MCP            qwen3.6:35b-a3b (coding)
```

See [HANDOFF.md](HANDOFF.md) for the full backstory, hardware, and history.

---

## Repo layout

| Path | What |
|------|------|
| `rocky_server.py` | Server (PC). Config-driven, keep-alive, memory, logging, dashboard. |
| `rocky_client.py` | Voice/text client (Mac). |
| `config.py` | Shared config + `.env` loader (one dep: PyYAML). |
| `config.example.yaml` | Copy to `config.yaml` and edit per machine. |
| `.env.example` | Copy to `.env` for Jira secrets (never committed). |
| `requirements-server.txt` / `-client.txt` / `-tts.txt` | Deps per role. |
| `scripts/windows/` | `start_rocky.bat`, `install_task.ps1` (auto-start at login). |
| `scripts/mac/` | `install_launchd.sh`, `start_rocky.sh`, launchd plists. |

---

## Quick start

### 1. Both machines — clone & configure

```bash
git clone <this-repo> rocky_ai && cd rocky_ai
cp config.example.yaml config.yaml      # edit IPs / models / paths
cp .env.example .env                     # add your Jira token (server only)
```

Get Tailscale IPs with `tailscale ip -4` and put them in `config.yaml`.
Create a Jira API token at
<https://id.atlassian.com/manage-profile/security/api-tokens>.

### 2. PC (Windows) — the server

```powershell
py -m pip install -r requirements-server.txt
# Ollama must be installed and serving on all interfaces:
#   set a SYSTEM env var  OLLAMA_HOST = 0.0.0.0:11434  then restart Ollama
ollama pull qwen3:30b-a3b
ollama pull qwen3.6:35b-a3b

py rocky_server.py                       # run in the foreground to test
```

Auto-start at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\install_task.ps1
Start-ScheduledTask -TaskName RockyServer
```

Visit `http://<pc-tailscale-ip>:7437/` for the status dashboard.

### 3. Mac — the client

```bash
python3 -m pip install -r requirements-client.txt
brew install ffmpeg whisper-cpp        # whisper-cli + ffmpeg

# Voice (rocky_say / Coqui XTTS) into its own venv:
python3.11 -m venv ~/.rocky_say/venv
~/.rocky_say/venv/bin/pip install -r requirements-tts.txt

scripts/mac/start_rocky.sh             # text mode
scripts/mac/start_rocky.sh --voice     # wake-word "Hey Rocky"
```

Auto-start the TTS server (and optionally the voice client) at login:

```bash
chmod +x scripts/mac/*.sh
scripts/mac/install_launchd.sh           # TTS server only
scripts/mac/install_launchd.sh --client  # also auto-run the voice client
```

---

## Using the general AI / coding agent

Rocky is a general assistant — ask anything; he only hits Jira when you mention a
ticket key (e.g. `PROJ-123`) or a keyword (`ticket`, `sprint`, …, configurable
under `jira.keywords`). Say **"forget"** to clear conversation memory.

For coding, point **Cursor** at the same Ollama instance (OpenAI-compatible):

- **Provider:** OpenAI-compatible
- **Base URL:** `http://<pc-tailscale-ip>:11434/v1`
- **API Key:** `ollama` (any non-empty string)
- **Model:** `qwen3.6:35b-a3b` (the `ollama.coding_model` in config)
- **MCP:** Atlassian Rovo — `npx -y mcp-remote@latest https://mcp.atlassian.com/v1/mcp`

---

## Efficiency notes

- **`keep_alive`** (config `ollama.keep_alive`, default `30m`) keeps the model
  resident in VRAM between requests, so replies stay fast instead of paying a
  cold reload each time. Set `-1` to never unload (fastest, holds ~all VRAM).
- **`no_think: true`** uses qwen3's `/no_think` fast path — no chain-of-thought,
  which is what you want for short spoken replies.
- **`num_ctx`** caps context size; lower it if VRAM is tight, raise it for longer
  memory/Jira context.
- **rocky_say persistent server** (auto-started via launchd) keeps XTTS loaded —
  ~3s per reply vs ~20s for the cold fallback.
- The server is **pure standard library + PyYAML** and threaded, so it stays
  light on the always-on PC.

---

## Security

Rocky has **no cloud component** — but the server does listen on a port, so:

- **Keep it behind Tailscale.** Don't port-forward 7437 on your router. Confirm
  the Windows Firewall blocks inbound 7437 on the *Public* and *Private*
  profiles (only the Tailscale interface should reach it). For the strongest
  isolation set `network.server_bind` to the PC's Tailscale IP so the server
  only accepts VPN traffic.
- **Optional shared secret.** Set `ROCKY_AUTH_TOKEN` (in `.env`) to the same long
  random value on both machines. When set, every query and the dashboard require
  it — a second lock in case the port is ever exposed. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **The dashboard shows recent queries.** It escapes them (no XSS) and is
  auth-gated when a token is set, but treat it as sensitive.
- **Never commit `.env` or `config.yaml`** — both are gitignored. Only the
  `*.example` files are tracked. Rotate your Jira token if it ever leaks.

The query path is constrained by design: Jira ticket keys are validated by regex
(no SSRF), oversized request bodies are rejected, and the client reads TTS text
from a file rather than interpolating it into code (no injection).

## Configuration reference

Everything lives in `config.yaml` (non-secret) and `.env` (secrets). Highlights:

| Key | Meaning |
|-----|---------|
| `ollama.voice_model` / `coding_model` | Models for Rocky vs Cursor. |
| `ollama.keep_alive` / `num_ctx` / `temperature` | Speed/quality tuning. |
| `rocky.persona` | Rocky's system prompt (`{user}` is substituted). |
| `memory.enabled` / `max_turns` | Conversation memory across turns & restarts. |
| `jira.keywords` | Words that trigger an automatic ticket lookup. |
| `voice.*` | Mac-only: whisper/ffmpeg/rocky_say paths, wake words. |
| `logging.*` | Rotating log file under `logs/`. |

Point `ROCKY_CONFIG` at a different file to override the location.

---

## Troubleshooting

- **Client can't reach server** — PC on? Tailscale up on both? Check
  `http://<pc-ip>:7437/health`.
- **Slow replies / model reloads each time** — bump `ollama.keep_alive`; confirm
  the model fits in VRAM.
- **TTS slow (~20s)** — rocky_say persistent server isn't running; start it with
  `rocky_say --server start` or install the launchd agent.
- **Wake word not firing** — needs `~/.whisper/ggml-tiny.en.bin`; falls back to
  text mode otherwise.
- **Jira errors** — token in `.env`? Uses `/rest/api/3/search/jql` (the old
  `/search` was removed by Atlassian).
- **Ollama not reachable on PC** — set the **system** env var
  `OLLAMA_HOST=0.0.0.0:11434` and restart the Ollama service; re-check after any
  Ollama update.

---

## Roadmap

Done here: config file, requirements, README, auto-start (both machines),
conversation memory, file logging, status dashboard. Still open from the
handoff: multi-user `USER_NAME` per client, more MCP integrations (GitHub,
Confluence, Slack) server-side, and a fast local wake-word model.
```
