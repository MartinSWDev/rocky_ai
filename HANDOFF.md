# Rocky — Claude Code Handoff Document
**Project**: Local AI voice assistant + coding agent  
**Owner**: Your Name (you@example.com)  
**Date**: April 2026

---

## What this project is

A fully local, private AI assistant called Rocky (voiced after the Eridian alien from Project Hail Mary). It runs on a home PC and is accessed from a MacBook over Tailscale VPN. No data is sent to third-party AI services.

Two main components:
1. **Rocky voice assistant** — speak to Rocky, he reads your Jira tickets and answers questions
2. **Cursor coding agent** — Cursor IDE uses local Ollama models + Atlassian Rovo MCP to read and implement Jira tickets

---

## Hardware

**Home PC (home-pc)**
- AMD Ryzen 9 9950X3D, 96GB RAM, RTX 5080 (16GB VRAM)
- Windows 11 + WSL2 (Ubuntu)
- Always on, Tailscale IP: `100.x.y.z`

**MacBook (laptop)**
- Apple M3, macOS Tahoe 26
- Tailscale IP: `100.x.y.w`
- Daily driver — all interaction happens here

---

## Architecture

```
MacBook
├── rocky_client.py        voice client
│   ├── ffmpeg             records mic (avfoundation)
│   ├── whisper-cli        transcribes speech to text
│   ├── HTTP POST          sends query to PC:7437 over Tailscale
│   └── rocky_say / afplay speaks response
│
└── Cursor IDE
    ├── Ollama API         http://100.x.y.z:11434/v1
    └── Atlassian Rovo MCP https://mcp.atlassian.com/v1/mcp

Tailscale VPN (encrypted tunnel)

PC (Windows — not WSL)
├── rocky_server.py        HTTP server on 0.0.0.0:7437
│   ├── Jira REST API      fetches assigned tickets
│   └── Ollama API         http://localhost:11434
│
└── Ollama (Windows service)
    ├── qwen3:30b-a3b      voice assistant model
    └── qwen3.6:35b-a3b    coding agent model
```

---

## Current file locations

### PC — Windows side
```
C:\Users\<you>\jarvis\rocky_server.py    main server
C:\Users\<you>\jarvis\start_rocky.bat   startup launcher (sets env vars)
```

### PC — WSL side (reference only, not actively used for serving)
```
~/jarvis/rocky_server.py                 WSL version (superseded by Windows version)
~/.config/mcp/jira.env                  Jira credentials
/usr/local/bin/rocky_say                rocky_say binary
~/.rocky_say/venv/                      Python 3.11 venv with Coqui TTS
~/.rocky_say/rocky_training_audio_scrubbed.wav
```

### Mac
```
~/jarvis/rocky_client.py                voice client
/usr/local/bin/rocky_say               rocky_say symlink
~/.rocky_say/venv/                     Python 3.11 venv with Coqui TTS
~/.rocky_say/rocky_training_audio_scrubbed.wav
~/.whisper/ggml-base.en.bin            Whisper model (actually large-v3 despite name)
```

---

## rocky_server.py — current working version (Windows)

```python
#!/usr/bin/env python3
"""
Rocky orchestration server — runs on Windows (not WSL)
Listens on port 7437 for queries from the Mac
Calls Ollama qwen3:30b-a3b with Jira context
Returns text response
"""

import json
import os
import base64
import urllib.request
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler

OLLAMA_HOST = "http://localhost:11434"
MODEL       = "qwen3:30b-a3b"
PORT        = 7437

JIRA_URL    = os.environ.get("JIRA_URL", "")
JIRA_USER   = os.environ.get("JIRA_USERNAME", "")
JIRA_TOKEN  = os.environ.get("JIRA_API_TOKEN", "")

USER_NAME   = "User"

ROCKY_SYSTEM = f"""/no_think
You are Rocky, the Eridian alien from Project Hail Mary.
You are talking to {USER_NAME}. Use their name occasionally — naturally, not every sentence.
You help {USER_NAME} with their work. You have access to their Jira tickets.
Speak in Rocky's style: drop articles (a, an, the), simplify grammar,
triple emphasis words (good good good, bad bad bad), add 'question?' to questions.
Keep answers brief and practical. No markdown, plain text only — this is spoken aloud."""

TICKET_KEYWORDS = {
    "ticket","jira","issue","sprint","priority","assigned",
    "todo","task","work","backlog","story","bug","feature","epic"
}

def fetch_jira_tickets():
    if not all([JIRA_URL, JIRA_USER, JIRA_TOKEN]):
        return "Jira credentials not configured."
    creds = base64.b64encode(f"{JIRA_USER}:{JIRA_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}
    jql = "assignee = currentUser() AND resolution = Unresolved ORDER BY priority ASC"
    url = f"{JIRA_URL}/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}&maxResults=10&fields=summary,priority,status,assignee"
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        issues = data.get("issues", [])
        if not issues:
            return "No open tickets found."
        lines = []
        for i in issues:
            key      = i["key"]
            fields   = i.get("fields", {})
            summary  = fields.get("summary", "No summary")
            priority = fields.get("priority", {}).get("name", "?")
            status   = fields.get("status", {}).get("name", "?")
            lines.append(f"{key} [{priority}] ({status}): {summary}")
        return "\n".join(lines)
    except Exception as e:
        return f"Jira fetch error: {e}"

def fetch_jira_ticket(ticket_key):
    if not all([JIRA_URL, JIRA_USER, JIRA_TOKEN]):
        return "Jira credentials not configured."
    creds = base64.b64encode(f"{JIRA_USER}:{JIRA_TOKEN}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Accept": "application/json"}
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    try:
        req = urllib.request.Request(url, headers=headers)
        resp = urllib.request.urlopen(req, timeout=10)
        data = json.loads(resp.read())
        fields = data["fields"]
        summary     = fields["summary"]
        status      = fields["status"]["name"]
        priority    = fields.get("priority", {}).get("name", "?")
        description = ""
        desc = fields.get("description")
        if desc and isinstance(desc, dict):
            for block in desc.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        description += inline.get("text", "") + " "
        return (f"{ticket_key} [{priority}] ({status})\n"
                f"Summary: {summary}\n"
                f"Description: {description[:500]}")
    except Exception as e:
        return f"Could not fetch {ticket_key}: {e}"

def ask_ollama(query, jira_context=""):
    system = ROCKY_SYSTEM
    if jira_context:
        system += f"\n\nCurrent Jira data:\n{jira_context}"

    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": query}
        ],
        "stream": False,
        "options": {"temperature": 0.7}
    }).encode()

    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=120)
    return json.loads(resp.read())["message"]["content"]

def process_query(query):
    query_lower = query.lower()
    jira_context = ""

    import re
    ticket_refs = re.findall(r'\b[A-Z]+-\d+\b', query)
    if ticket_refs:
        parts = []
        for ref in ticket_refs:
            parts.append(fetch_jira_ticket(ref))
        jira_context = "\n\n".join(parts)
    elif any(k in query_lower for k in TICKET_KEYWORDS):
        print(f"  → Fetching Jira tickets...")
        jira_context = fetch_jira_tickets()

    return ask_ollama(query, jira_context)

class RockyHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        print(f"  [{self.address_string()}] {format % args}")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body   = self.rfile.read(length).decode()
        try:
            data  = json.loads(body)
            query = data.get("query", "").strip()
        except Exception:
            query = body.strip()

        if not query:
            self.send_response(400)
            self.end_headers()
            return

        print(f"\nQuery: {query}")
        try:
            response = process_query(query)
            print(f"Rocky: {response}")
        except Exception as e:
            response = f"Error: {e}"
            print(response)

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"response": response}).encode())

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "ok",
                "model": MODEL,
                "jira": bool(JIRA_URL)
            }).encode())
        else:
            self.send_response(404)
            self.end_headers()

def main():
    print(f"Rocky server starting on port {PORT}")
    print(f"Model: {MODEL}")
    print(f"Jira: {JIRA_URL or 'not configured'}")
    server = HTTPServer(("0.0.0.0", PORT), RockyHandler)
    print(f"Ready. Listening for queries...")
    server.serve_forever()

if __name__ == "__main__":
    main()
```

---

## rocky_client.py — current working version (Mac)

```python
#!/usr/bin/env python3
"""
Rocky client — runs on Mac
Records mic → Whisper STT → sends to PC → speaks response
Supports text mode and --voice mode with wake word detection
"""

import subprocess
import json
import urllib.request
import tempfile
import os
import sys
import time

# ── CONFIG ────────────────────────────────────────────────
PC_IP             = "100.x.y.z"
PC_PORT           = 7437
RECORD_SECS       = 10
WHISPER_BIN       = "/opt/homebrew/bin/whisper-cli"
WHISPER_MODEL     = os.path.expanduser("~/.whisper/ggml-base.en.bin")  # actually large-v3
WHISPER_WAKE_MODEL= os.path.expanduser("~/.whisper/ggml-tiny.en.bin")  # fast wake word model
ROCKY_TTS_PORT    = 59720  # rocky_say persistent server
# ──────────────────────────────────────────────────────────

def check_server():
    try:
        resp = urllib.request.urlopen(
            f"http://{PC_IP}:{PC_PORT}/health", timeout=3
        )
        data = json.loads(resp.read())
        return data.get("status") == "ok"
    except Exception:
        return False

def record_audio(seconds=RECORD_SECS):
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", str(seconds), "-ar", "16000", "-ac", "1", tmp.name
    ], capture_output=True)
    return tmp.name

def transcribe(wav_path, model=None):
    if model is None:
        model = WHISPER_MODEL
    txt_path = wav_path + ".txt"
    subprocess.run([
        WHISPER_BIN, "--model", model,
        "--file", wav_path, "--no-timestamps",
        "--output-txt", "--output-file", wav_path,
    ], capture_output=True, text=True)
    if os.path.exists(txt_path):
        text = open(txt_path).read().strip()
        os.unlink(txt_path)
        return text
    return ""

def ask_rocky(query):
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"http://{PC_IP}:{PC_PORT}",
        data=payload,
        headers={"Content-Type": "application/json"}
    )
    resp = urllib.request.urlopen(req, timeout=120)
    data = json.loads(resp.read())
    return data.get("response", "")

def speak(text):
    # Try rocky_say persistent server first (fast ~3s)
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{ROCKY_TTS_PORT}",
            data=payload,
            headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=120)
        wav = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            tmp = f.name
        subprocess.run(["afplay", tmp])
        os.unlink(tmp)
        return
    except Exception as e:
        print(f"TTS server failed: {e}, falling back to standalone")

    # Fallback — spawn venv python (~20s, loads model fresh)
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    venv_python = os.path.expanduser("~/.rocky_say/venv/bin/python3")
    reference   = os.path.expanduser("~/.rocky_say/rocky_training_audio_scrubbed.wav")
    script = f"""
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["OMP_NUM_THREADS"] = "1"
from TTS.api import TTS
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
tts.tts_to_file(text=\"\"\"{text}\"\"\", speaker_wav="{reference}", language="en", file_path="{tmp}")
"""
    subprocess.run([venv_python, "-c", script], capture_output=True)
    subprocess.run(["afplay", tmp])
    os.unlink(tmp)

def listen_for_wake_word():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", "3", "-ar", "16000", "-ac", "1", tmp
    ], capture_output=True)
    result = subprocess.run([
        WHISPER_BIN, "--model", WHISPER_WAKE_MODEL,
        "--file", tmp, "--no-timestamps",
    ], capture_output=True, text=True)
    os.unlink(tmp)
    transcript = result.stdout.lower()
    return any(phrase in transcript for phrase in [
        "hey rocky", "hey, rocky", "hi rocky", "hello rocky", "okay rocky"
    ])

def record_until_silence(max_secs=10):
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", str(max_secs), "-ar", "16000", "-ac", "1",
        "-af", "silencedetect=noise=-30dB:d=1.5", tmp
    ], capture_output=True)
    return tmp

def main():
    voice_mode = "--voice" in sys.argv or "-v" in sys.argv

    print("Rocky client starting...")
    print(f"PC: {PC_IP}:{PC_PORT}")

    if not check_server():
        print(f"Cannot reach Rocky server at {PC_IP}:{PC_PORT}")
        print("Check: PC is on, Tailscale connected, server running")
        sys.exit(1)

    print("Connected to Rocky server")
    speak("Rocky online. Hello User. Ask question, question?")

    if voice_mode:
        print("\nListening for 'Hey Rocky'...")
        while True:
            try:
                if not listen_for_wake_word():
                    continue

                print("Wake word detected!")
                speak("Yes question?")

                print("Listening for your question (up to 10s)...")
                wav = record_until_silence(max_secs=10)
                query = transcribe(wav)
                os.unlink(wav)

                if not query or len(query.strip()) < 3:
                    print("(nothing heard)")
                    continue

                if query.lower().strip() in ("hey rocky", "hi rocky", "hello rocky"):
                    speak("Yes question?")
                    continue

                print(f"You: {query}")
                print("Rocky thinking...")
                t0 = time.time()
                response = ask_rocky(query)
                elapsed = time.time() - t0
                print(f"Rocky ({elapsed:.1f}s): {response}")
                speak(response)
                print("\nListening for 'Hey Rocky'...")

            except KeyboardInterrupt:
                print("\nStopped.")
                speak("Goodbye User.")
                break
            except Exception as e:
                print(f"Error: {e}")
                continue
    else:
        while True:
            try:
                query = input("\nYou: ").strip()
                if not query:
                    continue
                if query.lower() in ("quit", "exit", "bye", "goodbye"):
                    speak("See you later User. But Rocky no actually see you later.")
                    break
                print("Rocky thinking...")
                t0 = time.time()
                response = ask_rocky(query)
                elapsed = time.time() - t0
                print(f"Rocky ({elapsed:.1f}s): {response}")
                speak(response)
            except KeyboardInterrupt:
                print("\nStopped.")
                break
```

---

## start_rocky.bat — Windows launcher

```bat
@echo off
set JIRA_URL=https://your-domain.atlassian.net
set JIRA_USERNAME=you@example.com
set JIRA_API_TOKEN=YOUR_TOKEN_HERE
py C:\Users\<you>\jarvis\rocky_server.py
```

---

## Environment & dependencies

### PC (Windows)
- Python 3.11 (`py` command)
- Ollama (Windows service, `OLLAMA_HOST=0.0.0.0:11434` system env var)
- Models pulled: `qwen3:30b-a3b`, `qwen3.6:35b-a3b`
- Task Scheduler runs `start_rocky.bat` at login
- Tailscale installed, always running

### Mac
- Python 3.11 via Homebrew
- `whisper-cli` via Homebrew (at `/opt/homebrew/bin/whisper-cli`)
- `ffmpeg` via Homebrew
- `~/.rocky_say/venv/` — Python 3.11 venv with `TTS`, `transformers==4.44.0`, `torch==2.5.1`, `torchaudio==2.5.1`
- Whisper model: `~/.whisper/ggml-base.en.bin` (actually large-v3, mislabelled)
- Tiny model for wake word still downloading: `~/.whisper/ggml-tiny.en.bin`
- Tailscale installed, always running
- rocky_say server starts manually: `rocky_say --server start`

### Cursor (Mac)
- Provider: OpenAI-compatible
- Base URL: `http://100.x.y.z:11434/v1`
- API Key: `ollama`
- Model: `qwen3.6:35b-a3b`
- MCP: Atlassian Rovo MCP via `mcp-remote@latest https://mcp.atlassian.com/v1/mcp`

---

## Known issues / TODO

- **Wake word not working yet** — `ggml-tiny.en.bin` still downloading. Once done, `listen_for_wake_word()` uses it for fast 3s polling. Current workaround: run in text mode.
- **rocky_say server on Mac** — needs to be started manually with `rocky_say --server start` before running client. If not running, client falls back to slow standalone mode (~20s per response). Should be auto-started.
- **WSL port forwarding** — Rocky server was moved to Windows-native Python to avoid WSL→Windows port proxy issues. The WSL version of `rocky_server.py` still exists but is not used.
- **OLLAMA_HOST on Windows** — set as a System environment variable via Advanced System Settings. If Ollama is updated or reinstalled, verify this is still set.
- **Jira API** — uses `/rest/api/3/search/jql` (new endpoint). The old `/rest/api/3/search` has been removed by Atlassian.

## What to build next for the repo

- Proper `requirements.txt` / `pyproject.toml` for both server and client
- `.env.example` file for credentials (never commit real `.env`)
- `README.md` with quick start
- Auto-start rocky_say server on Mac login (launchd plist)
- Config file (`config.yaml`) instead of hardcoded values in scripts
- Multi-user support (different `USER_NAME` per client)
- Conversation history / memory across sessions
- More MCP integrations (GitHub, Confluence, Slack) on the server side
- Proper logging to file instead of stdout
- Health check dashboard (simple web UI showing server status, model loaded, Jira connected)
