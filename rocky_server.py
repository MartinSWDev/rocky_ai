#!/usr/bin/env python3
"""Rocky orchestration server — runs on the PC (Windows-native, not WSL).

Listens for queries from the Mac, optionally enriches them with Jira context,
and asks the local Ollama model. Rocky is a general assistant: any query works,
Jira lookups only kick in when a ticket is referenced or a keyword appears.

Features beyond the original handoff script:
  * all settings come from config.yaml / .env (config.py)
  * keep_alive keeps the model resident in VRAM for fast replies
  * rolling conversation memory (persisted across restarts)
  * rotating file logging
  * a tiny status dashboard at GET /
"""
from __future__ import annotations

import base64
import hmac
import html
import json
import logging
import os
import re
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import expand, load_config

CFG = load_config()

OLLAMA_HOST   = CFG.get("ollama.host", "http://localhost:11434")
MODEL         = CFG.get("ollama.voice_model", "qwen3:30b-a3b")
TEMPERATURE   = CFG.get("ollama.temperature", 0.7)
NUM_CTX       = CFG.get("ollama.num_ctx", 8192)
KEEP_ALIVE    = CFG.get("ollama.keep_alive", "30m")
REQ_TIMEOUT   = CFG.get("ollama.request_timeout", 120)

PORT          = CFG.get("network.server_port", 7437)
BIND          = CFG.get("network.server_bind", "0.0.0.0")
# Optional shared secret (defense-in-depth on top of Tailscale). Prefer the env
# var so it never lands in a committed config file. Empty = auth disabled.
AUTH_TOKEN    = (os.environ.get("ROCKY_AUTH_TOKEN") or CFG.get("network.auth_token") or "")
MAX_BODY      = CFG.get("network.max_body_bytes", 65536)

USER_NAME     = CFG.get("rocky.user_name", "User")
NO_THINK      = CFG.get("rocky.no_think", True)
PERSONA       = CFG.get("rocky.persona", "You are Rocky, a helpful assistant talking to {user}.")

JIRA_ENABLED  = CFG.get("jira.enabled", True)
JIRA_URL      = CFG.get("jira.url", "")
JIRA_USER     = os.environ.get("JIRA_USERNAME", "")
JIRA_TOKEN    = os.environ.get("JIRA_API_TOKEN", "")
JIRA_MAX      = CFG.get("jira.max_results", 10)
TICKET_KEYWORDS = set(CFG.get("jira.keywords", []))

MEM_ENABLED   = CFG.get("memory.enabled", True)
MEM_MAX_TURNS = CFG.get("memory.max_turns", 12)
MEM_PATH      = Path(expand(CFG.get("memory.store_path", ".rocky_memory.jsonl")))

DASH_ENABLED  = CFG.get("dashboard.enabled", True)

# NB: the `/no_think` prompt token is ignored by qwen3 in Ollama — thinking is
# actually disabled via the `think: false` API param in _ollama_payload().
SYSTEM_PROMPT = PERSONA.format(user=USER_NAME).strip()

# ── logging ──────────────────────────────────────────────────────────────────
def _setup_logging() -> logging.Logger:
    log = logging.getLogger("rocky")
    log.setLevel(CFG.get("logging.level", "INFO"))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S")

    console = logging.StreamHandler()
    console.setFormatter(fmt)
    log.addHandler(console)

    log_file = expand(CFG.get("logging.file", "logs/rocky.log"))
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        fileh = RotatingFileHandler(
            log_file,
            maxBytes=CFG.get("logging.max_bytes", 1_048_576),
            backupCount=CFG.get("logging.backup_count", 3),
            encoding="utf-8",
        )
        fileh.setFormatter(fmt)
        log.addHandler(fileh)
    return log


LOG = _setup_logging()


# ── conversation memory ──────────────────────────────────────────────────────
class Memory:
    """Single-user rolling history, persisted as JSONL so it survives restarts."""

    def __init__(self, path: Path, max_turns: int):
        self.path = path
        self.max_turns = max_turns
        self.turns: deque = deque(maxlen=max_turns * 2)  # user + assistant per turn
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line:
                    self.turns.append(json.loads(line))
        except Exception as exc:  # corrupt store shouldn't be fatal
            LOG.warning("Could not load memory: %s", exc)

    def add(self, role: str, content: str) -> None:
        self.turns.append({"role": role, "content": content})
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({"role": role, "content": content}) + "\n")
        except Exception as exc:
            LOG.warning("Could not persist memory: %s", exc)

    def messages(self) -> list:
        return list(self.turns)

    def clear(self) -> None:
        self.turns.clear()
        try:
            self.path.unlink(missing_ok=True)
        except Exception:
            pass


MEMORY = Memory(MEM_PATH, MEM_MAX_TURNS) if MEM_ENABLED else None

# Recent activity for the dashboard.
RECENT: deque = deque(maxlen=20)
START_TIME = time.time()


# ── Jira ─────────────────────────────────────────────────────────────────────
def _jira_headers() -> dict:
    creds = base64.b64encode(f"{JIRA_USER}:{JIRA_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {creds}", "Accept": "application/json"}


def _jira_ready() -> bool:
    return bool(JIRA_ENABLED and JIRA_URL and JIRA_USER and JIRA_TOKEN)


def fetch_jira_tickets() -> str:
    if not _jira_ready():
        return "Jira credentials not configured."
    jql = "assignee = currentUser() AND resolution = Unresolved ORDER BY priority ASC"
    # /rest/api/3/search/jql is the current endpoint; the old /search was removed.
    url = (
        f"{JIRA_URL}/rest/api/3/search/jql?jql={urllib.parse.quote(jql)}"
        f"&maxResults={JIRA_MAX}&fields=summary,priority,status,assignee"
    )
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=_jira_headers()), timeout=10)
        issues = json.loads(resp.read()).get("issues", [])
        if not issues:
            return "No open tickets found."
        lines = []
        for i in issues:
            f = i.get("fields", {})
            lines.append(
                f"{i['key']} [{f.get('priority', {}).get('name', '?')}] "
                f"({f.get('status', {}).get('name', '?')}): {f.get('summary', 'No summary')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        LOG.error("Jira list error: %s", exc)
        return f"Jira fetch error: {exc}"


def fetch_jira_ticket(ticket_key: str) -> str:
    if not _jira_ready():
        return "Jira credentials not configured."
    url = f"{JIRA_URL}/rest/api/3/issue/{ticket_key}"
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, headers=_jira_headers()), timeout=10)
        fields = json.loads(resp.read())["fields"]
        description = ""
        desc = fields.get("description")
        if isinstance(desc, dict):
            for block in desc.get("content", []):
                for inline in block.get("content", []):
                    if inline.get("type") == "text":
                        description += inline.get("text", "") + " "
        return (
            f"{ticket_key} [{fields.get('priority', {}).get('name', '?')}] "
            f"({fields['status']['name']})\n"
            f"Summary: {fields['summary']}\n"
            f"Description: {description[:500]}"
        )
    except Exception as exc:
        LOG.error("Jira fetch %s error: %s", ticket_key, exc)
        return f"Could not fetch {ticket_key}: {exc}"


# ── Ollama ───────────────────────────────────────────────────────────────────
def _build_system(jira_context: str) -> str:
    system = SYSTEM_PROMPT
    if jira_context:
        # Mark live data as authoritative so stale "no tickets" turns in memory
        # can't make the model contradict a fresh, successful fetch.
        system += (
            "\n\nCurrent Jira data below is LIVE and authoritative. Trust it over "
            "anything said earlier in this conversation; earlier replies may be "
            f"out of date.\n{jira_context}"
        )
    return system


def _build_messages(system: str, query: str) -> list:
    messages = [{"role": "system", "content": system}]
    if MEMORY:
        messages.extend(MEMORY.messages())
    messages.append({"role": "user", "content": query})
    return messages


def _record(query: str, answer: str) -> None:
    if MEMORY:
        MEMORY.add("user", query)
        MEMORY.add("assistant", answer)
    RECENT.append({
        "t": time.strftime("%H:%M:%S"),
        "q": (query[:80] + "…") if len(query) > 80 else query,
        "a": (answer[:80] + "…") if len(answer) > 80 else answer,
    })


def _ollama_payload(query: str, jira_context: str, stream: bool) -> bytes:
    body = {
        "model": MODEL,
        "messages": _build_messages(_build_system(jira_context), query),
        "stream": stream,
        "keep_alive": KEEP_ALIVE,
        "options": {"temperature": TEMPERATURE, "num_ctx": NUM_CTX},
    }
    if NO_THINK:
        # The decisive latency fix: skips qwen3's chain-of-thought (hundreds of
        # tokens) and emits only the spoken answer (~10-20 tokens).
        body["think"] = False
    return json.dumps(body).encode()


def ask_ollama(query: str, jira_context: str = "") -> str:
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=_ollama_payload(query, jira_context, False),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
    answer = json.loads(resp.read())["message"]["content"].strip()
    _record(query, answer)
    return answer


def ask_ollama_stream(query: str, jira_context: str = ""):
    """Yield response text as Ollama produces it; record the full answer at end.

    Lets the client start speaking the first sentence while the rest is still
    generating — the single biggest win for perceived voice latency.
    """
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/chat",
        data=_ollama_payload(query, jira_context, True),
        headers={"Content-Type": "application/json"},
    )
    resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
    parts = []
    for raw in resp:                       # Ollama streams newline-delimited JSON
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        chunk = obj.get("message", {}).get("content", "")
        if chunk:
            parts.append(chunk)
            yield chunk
        if obj.get("done"):
            break
    _record(query, "".join(parts).strip())


def _maybe_jira(query: str) -> str:
    if not JIRA_ENABLED:
        return ""
    ticket_refs = re.findall(r"\b[A-Z]+-\d+\b", query)
    if ticket_refs:
        return "\n\n".join(fetch_jira_ticket(ref) for ref in ticket_refs)
    if any(k in query.lower() for k in TICKET_KEYWORDS):
        LOG.info("  -> Fetching Jira tickets...")
        return fetch_jira_tickets()
    return ""


def _is_forget(query: str) -> bool:
    return query.lower().strip() in ("forget", "reset memory", "clear memory")


def process_query(query: str) -> str:
    if _is_forget(query) and MEMORY:
        MEMORY.clear()
        return "Memory cleared. Fresh start fresh start."
    return ask_ollama(query, _maybe_jira(query))


def process_query_stream(query: str):
    if _is_forget(query) and MEMORY:
        MEMORY.clear()
        yield "Memory cleared. Fresh start fresh start."
        return
    yield from ask_ollama_stream(query, _maybe_jira(query))


# ── HTTP ─────────────────────────────────────────────────────────────────────
def _dashboard_html() -> str:
    up = int(time.time() - START_TIME)
    hrs, rem = divmod(up, 3600)
    mins, secs = divmod(rem, 60)
    rows = "".join(
        f"<tr><td>{r['t']}</td><td>{html.escape(r['q'])}</td><td>{html.escape(r['a'])}</td></tr>"
        for r in reversed(RECENT)
    ) or "<tr><td colspan=3><em>no queries yet</em></td></tr>"
    return f"""<!doctype html><html><head><meta charset=utf-8>
<title>Rocky status</title>
<style>
 body{{font-family:system-ui,sans-serif;max-width:820px;margin:2rem auto;padding:0 1rem;color:#222}}
 h1{{margin-bottom:.2rem}} .ok{{color:#127c2b;font-weight:600}} .off{{color:#999}}
 table{{border-collapse:collapse;width:100%;margin-top:1rem;font-size:.9rem}}
 td,th{{border:1px solid #ddd;padding:.4rem .6rem;text-align:left;vertical-align:top}}
 .grid{{display:grid;grid-template-columns:max-content 1fr;gap:.2rem 1rem;margin:1rem 0}}
 code{{background:#f4f4f4;padding:.1rem .3rem;border-radius:3px}}
</style></head><body>
<h1>🪨 Rocky</h1><p class=ok>● online</p>
<div class=grid>
 <b>Model</b><code>{MODEL}</code>
 <b>Ollama</b><code>{OLLAMA_HOST}</code>
 <b>keep_alive</b><code>{KEEP_ALIVE}</code>
 <b>Jira</b><span class="{'ok' if _jira_ready() else 'off'}">{'connected' if _jira_ready() else 'not configured'}</span>
 <b>Memory</b><span>{'on, ' + str(len(MEMORY.turns)//2) + ' turns' if MEMORY else 'off'}</span>
 <b>Uptime</b><span>{hrs}h {mins}m {secs}s</span>
</div>
<h3>Recent queries</h3>
<table><tr><th>time</th><th>query</th><th>reply</th></tr>{rows}</table>
</body></html>"""


class RockyHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # route stdlib logs through our logger
        LOG.debug("[%s] %s", self.address_string(), fmt % args)

    def _send_json(self, code: int, obj: dict):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        """True if auth is disabled, or a valid token is presented.

        Accepts ``Authorization: Bearer <token>`` (clients) or ``?token=`` in the
        URL (so the dashboard works from a browser). Constant-time comparison.
        """
        if not AUTH_TOKEN:
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Bearer ") and hmac.compare_digest(header[7:], AUTH_TOKEN):
            return True
        token = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("token", [""])[0]
        return hmac.compare_digest(token, AUTH_TOKEN)

    def do_POST(self):
        if not self._authorized():
            self.send_response(401)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        if length > MAX_BODY:
            self.send_response(413)  # payload too large
            self.end_headers()
            return
        body = self.rfile.read(length).decode()
        stream = False
        try:
            data = json.loads(body)
            query = data.get("query", "").strip()
            stream = bool(data.get("stream"))
        except Exception:
            query = body.strip()

        if not query:
            self.send_response(400)
            self.end_headers()
            return

        LOG.info("Query: %s", query)

        if stream:
            # Newline-delimited JSON: one {"chunk": "..."} per token batch.
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()
            parts = []
            try:
                for chunk in process_query_stream(query):
                    parts.append(chunk)
                    self.wfile.write((json.dumps({"chunk": chunk}) + "\n").encode())
                    self.wfile.flush()
            except Exception as exc:
                LOG.exception("stream failed")
                try:
                    self.wfile.write((json.dumps({"chunk": f"Error: {exc}"}) + "\n").encode())
                except Exception:
                    pass
            LOG.info("Rocky (stream): %s", "".join(parts))
            return

        try:
            response = process_query(query)
            LOG.info("Rocky: %s", response)
        except Exception as exc:
            response = f"Error: {exc}"
            LOG.exception("process_query failed")
        self._send_json(200, {"response": response})

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "ok",
                "model": MODEL,
                "jira": _jira_ready(),
                "memory": bool(MEMORY),
                "uptime_secs": int(time.time() - START_TIME),
            })
        elif urllib.parse.urlparse(self.path).path == "/" and DASH_ENABLED:
            if not self._authorized():
                self.send_response(401)
                self.end_headers()
                return
            body = _dashboard_html().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


def main():
    LOG.info("Rocky server starting on %s:%s", BIND, PORT)
    LOG.info("Model: %s (keep_alive=%s)", MODEL, KEEP_ALIVE)
    LOG.info("Jira: %s", JIRA_URL if _jira_ready() else "not configured")
    LOG.info("Memory: %s | Dashboard: http://%s:%s/", "on" if MEMORY else "off",
             CFG.get("network.pc_tailscale_ip", "localhost"), PORT)
    server = ThreadingHTTPServer((BIND, PORT), RockyHandler)
    LOG.info("Ready. Listening for queries...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        LOG.info("Shutting down.")


if __name__ == "__main__":
    main()
