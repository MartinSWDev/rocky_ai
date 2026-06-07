#!/usr/bin/env python3
"""Rocky voice client — runs on the Mac.

Records the mic -> Whisper STT -> sends the query to the PC over Tailscale ->
speaks Rocky's reply with rocky_say (Coqui XTTS). Two modes:

  python3 rocky_client.py            text mode (type questions)
  python3 rocky_client.py --voice    voice mode (wake word "Hey Rocky")

Latency-focused design:
  * recording stops when you stop talking (energy VAD), not after a fixed 10s;
  * transcription can use a warm whisper.cpp server instead of cold-loading;
  * the reply is streamed and spoken sentence-by-sentence, so Rocky starts
    talking ~1-2s in instead of waiting for the whole answer + whole synth.

All paths/IPs come from config.yaml (see config.example.yaml).
"""
from __future__ import annotations

import array
import json
import math
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

from config import expand, load_config

CFG = load_config()

PC_IP        = CFG.get("network.pc_tailscale_ip", "100.x.y.z")
PC_PORT      = CFG.get("network.server_port", 7437)
REQ_TIMEOUT  = CFG.get("ollama.request_timeout", 120)

RECORD_SECS  = CFG.get("voice.record_secs", 10)
SILENCE_SECS = CFG.get("voice.silence_secs", 1.5)
SILENCE_RMS  = CFG.get("voice.silence_rms", 500)
WHISPER_BIN  = expand(CFG.get("voice.whisper_bin", "/opt/homebrew/bin/whisper-cli"))
WHISPER_MODEL      = expand(CFG.get("voice.whisper_model", "~/.whisper/ggml-small.en.bin"))
WHISPER_WAKE_MODEL = expand(CFG.get("voice.whisper_wake_model", "~/.whisper/ggml-tiny.en.bin"))
WHISPER_SERVER_URL = CFG.get("voice.whisper_server_url", "")   # e.g. http://127.0.0.1:8910/inference
WAKE_WORDS   = [w.lower() for w in CFG.get("voice.wake_words", ["hey rocky"])]

STREAM       = CFG.get("voice.stream", True)
TTS_PORT     = CFG.get("voice.tts_port", 59720)
TTS_REF      = expand(CFG.get("voice.tts_reference", "~/.rocky_say/rocky_training_audio_scrubbed.wav"))
TTS_PYTHON   = expand(CFG.get("voice.tts_venv_python", "~/.rocky_say/venv/bin/python3"))

USER_NAME    = CFG.get("rocky.user_name", "User")
AUTH_TOKEN   = (os.environ.get("ROCKY_AUTH_TOKEN") or CFG.get("network.auth_token") or "")


# ── server ───────────────────────────────────────────────────────────────────
def _auth_headers(base: dict | None = None) -> dict:
    headers = dict(base or {})
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    return headers


def check_server() -> bool:
    try:
        resp = urllib.request.urlopen(f"http://{PC_IP}:{PC_PORT}/health", timeout=3)
        return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


def ask_rocky(query: str) -> str:
    """Non-streaming request (used as a fallback)."""
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"http://{PC_IP}:{PC_PORT}", data=payload,
        headers=_auth_headers({"Content-Type": "application/json"}),
    )
    resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
    return json.loads(resp.read()).get("response", "")


def ask_rocky_stream(query: str):
    """Yield reply text chunks as the server streams them (NDJSON)."""
    payload = json.dumps({"query": query, "stream": True}).encode()
    req = urllib.request.Request(
        f"http://{PC_IP}:{PC_PORT}", data=payload,
        headers=_auth_headers({"Content-Type": "application/json"}),
    )
    resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
    for raw in resp:                       # one JSON object per line
        raw = raw.strip()
        if not raw:
            continue
        try:
            chunk = json.loads(raw).get("chunk", "")
        except Exception:
            continue
        if chunk:
            yield chunk


# ── recording ────────────────────────────────────────────────────────────────
def _record_fixed(seconds: float) -> str:
    """Record a fixed-length clip (used for wake-word polling)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", str(seconds), "-ar", "16000", "-ac", "1", tmp.name,
    ], capture_output=True)
    return tmp.name


def record_until_silence(max_secs: float | None = None) -> str:
    """Record from the mic and STOP shortly after the speaker goes quiet.

    Streams raw PCM from ffmpeg and tracks per-chunk loudness (RMS). Once speech
    has started, `SILENCE_SECS` of quiet ends the take. Caps at `max_secs`. This
    is the fix for the old behaviour that always recorded a full 10 seconds.
    """
    max_secs = max_secs or RECORD_SECS
    rate = 16000
    chunk_s = 0.03                          # 30 ms frames
    chunk_bytes = int(rate * chunk_s) * 2   # s16le mono

    proc = subprocess.Popen([
        "ffmpeg", "-hide_banner", "-loglevel", "quiet",
        "-f", "avfoundation", "-i", ":0",
        "-ar", str(rate), "-ac", "1", "-f", "s16le", "-",
    ], stdout=subprocess.PIPE)

    frames = bytearray()
    elapsed = silence = 0.0
    started = False
    try:
        while elapsed < max_secs:
            buf = proc.stdout.read(chunk_bytes)
            if not buf or len(buf) < chunk_bytes:
                break
            frames.extend(buf)
            elapsed += chunk_s
            samples = array.array("h")
            samples.frombytes(buf)
            rms = math.sqrt(sum(s * s for s in samples) / len(samples)) if samples else 0.0
            if rms >= SILENCE_RMS:
                started, silence = True, 0.0
            elif started:
                silence += chunk_s
                if silence >= SILENCE_SECS:
                    break
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=1)
        except Exception:
            proc.kill()

    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    import wave
    with wave.open(tmp.name, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return tmp.name


# ── transcription ────────────────────────────────────────────────────────────
def _whisper_via_server(wav_path: str) -> str:
    """POST audio to a warm whisper.cpp server (/inference) — no model reload."""
    with open(wav_path, "rb") as f:
        audio = f.read()
    boundary = "----rocky" + os.urandom(8).hex()
    bnd = boundary.encode()

    def field(name: str, value: str) -> bytes:
        return (b"--" + bnd + b"\r\nContent-Disposition: form-data; name=\""
                + name.encode() + b"\"\r\n\r\n" + value.encode() + b"\r\n")

    body = field("temperature", "0.0") + field("response_format", "json")
    body += (b"--" + bnd + b"\r\nContent-Disposition: form-data; name=\"file\"; "
             b"filename=\"a.wav\"\r\nContent-Type: audio/wav\r\n\r\n" + audio + b"\r\n")
    body += b"--" + bnd + b"--\r\n"

    req = urllib.request.Request(
        WHISPER_SERVER_URL, data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    raw = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "ignore").strip()
    try:
        return json.loads(raw).get("text", "").strip()
    except Exception:
        return raw


def transcribe(wav_path: str, model: str | None = None) -> str:
    if WHISPER_SERVER_URL:
        try:
            return _whisper_via_server(wav_path)
        except Exception as exc:
            print(f"whisper server failed: {exc}; falling back to CLI")
    model = model or WHISPER_MODEL
    txt_path = wav_path + ".txt"
    subprocess.run([
        WHISPER_BIN, "--model", model,
        "--file", wav_path, "--no-timestamps",
        "--output-txt", "--output-file", wav_path,
    ], capture_output=True, text=True)
    if os.path.exists(txt_path):
        text = open(txt_path, encoding="utf-8", errors="ignore").read().strip()
        os.unlink(txt_path)
        return text
    return ""


# ── text to speech ───────────────────────────────────────────────────────────
def _tts_server_wav(text: str) -> str | None:
    """Synthesize via the persistent rocky_say server (fast, ~3s). None on fail."""
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{TTS_PORT}", data=payload,
            headers={"Content-Type": "application/json"},
        )
        wav = urllib.request.urlopen(req, timeout=REQ_TIMEOUT).read()
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(wav)
        f.close()
        return f.name
    except Exception as exc:
        print(f"TTS server failed: {exc}, falling back to standalone")
        return None


def _tts_fallback_wav(text: str) -> str | None:
    """Slow fallback: spawn the venv python and load XTTS fresh (~20s).

    Reply text is read from a file inside the subprocess — never interpolated
    into the script — so a crafted response cannot inject code.
    """
    out = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    out.close()
    tf = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8")
    tf.write(text)
    tf.close()
    script = f"""
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["OMP_NUM_THREADS"] = "1"
from TTS.api import TTS
text = open({tf.name!r}, encoding="utf-8").read()
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
tts.tts_to_file(text=text, speaker_wav={TTS_REF!r}, language="en", file_path={out.name!r})
"""
    subprocess.run([TTS_PYTHON, "-c", script], capture_output=True)
    os.unlink(tf.name)
    if os.path.exists(out.name) and os.path.getsize(out.name) > 0:
        return out.name
    return None


def synth_wav(text: str) -> str | None:
    return _tts_server_wav(text) or _tts_fallback_wav(text)


def play_wav(path: str | None) -> None:
    if not path:
        return
    subprocess.run(["afplay", path])
    try:
        os.unlink(path)
    except Exception:
        pass


def speak(text: str) -> None:
    play_wav(synth_wav(text))


# ── streaming reply pipeline ─────────────────────────────────────────────────
_SENTENCE_END = re.compile(r"[.!?](\s|$)|\n")


def _sentences(chunk_iter):
    """Group streamed text chunks into speakable sentences."""
    buf = ""
    for chunk in chunk_iter:
        buf += chunk
        while True:
            m = _SENTENCE_END.search(buf)
            if not m:
                break
            sent = buf[:m.end()].strip()
            buf = buf[m.end():]
            if sent:
                yield sent
    if buf.strip():
        yield buf.strip()


def speak_stream(sentence_iter) -> None:
    """Synthesize the next sentence while the current one is playing.

    A worker thread turns sentences into wav files and queues them; the main
    thread plays them in order. Overlapping synth with playback is what makes
    the reply feel responsive.
    """
    wavq: queue.Queue = queue.Queue(maxsize=3)
    err: dict = {}

    def producer():
        try:
            for sent in sentence_iter:
                wavq.put(synth_wav(sent))
        except Exception as exc:
            err["exc"] = exc
        finally:
            wavq.put(None)

    t = threading.Thread(target=producer, daemon=True)
    t.start()
    while True:
        wav = wavq.get()
        if wav is None:
            break
        play_wav(wav)
    t.join()
    if err:
        raise err["exc"]


def respond(query: str) -> str:
    """Get Rocky's reply and speak it — streamed when enabled, else one-shot."""
    if STREAM:
        collected: list[str] = []

        def sentences():
            for sent in _sentences(ask_rocky_stream(query)):
                print(f"Rocky: {sent}")
                collected.append(sent)
                yield sent

        try:
            speak_stream(sentences())
            return " ".join(collected)
        except Exception as exc:
            if collected:
                print(f"(stream interrupted: {exc})")
                return " ".join(collected)
            print(f"(stream failed: {exc}; using non-streaming)")

    t0 = time.time()
    reply = ask_rocky(query)
    print(f"Rocky ({time.time() - t0:.1f}s): {reply}")
    speak(reply)
    return reply


# ── wake word ────────────────────────────────────────────────────────────────
def listen_for_wake_word() -> bool:
    wav = _record_fixed(3)
    text = transcribe(wav, model=WHISPER_WAKE_MODEL).lower()
    try:
        os.unlink(wav)
    except Exception:
        pass
    return any(phrase in text for phrase in WAKE_WORDS)


# ── main ─────────────────────────────────────────────────────────────────────
def main():
    voice_mode = "--voice" in sys.argv or "-v" in sys.argv

    print("Rocky client starting...")
    print(f"PC: {PC_IP}:{PC_PORT}  (stream={'on' if STREAM else 'off'}, "
          f"whisper={'server' if WHISPER_SERVER_URL else 'cli'})")

    if not check_server():
        print(f"Cannot reach Rocky server at {PC_IP}:{PC_PORT}")
        print("Check: PC is on, Tailscale connected, server running")
        sys.exit(1)

    print("Connected to Rocky server")
    speak(f"Rocky online. Hello {USER_NAME}. Ask question, question?")

    if voice_mode:
        print("\nListening for 'Hey Rocky'...")
        while True:
            try:
                if not listen_for_wake_word():
                    continue

                print("Wake word detected!")
                speak("Yes question?")

                print(f"Listening for your question (up to {RECORD_SECS}s)...")
                wav = record_until_silence(max_secs=RECORD_SECS)
                query = transcribe(wav)
                os.unlink(wav)

                if not query or len(query.strip()) < 3:
                    print("(nothing heard)")
                    continue
                if query.lower().strip() in WAKE_WORDS:
                    speak("Yes question?")
                    continue

                print(f"You: {query}")
                respond(query)
                print("\nListening for 'Hey Rocky'...")

            except KeyboardInterrupt:
                print("\nStopped.")
                speak(f"Goodbye {USER_NAME}.")
                break
            except Exception as exc:
                print(f"Error: {exc}")
                continue
    else:
        while True:
            try:
                query = input("\nYou: ").strip()
                if not query:
                    continue
                if query.lower() in ("quit", "exit", "bye", "goodbye"):
                    speak(f"See you later {USER_NAME}. But Rocky no actually see you later.")
                    break
                respond(query)
            except KeyboardInterrupt:
                print("\nStopped.")
                break


if __name__ == "__main__":
    main()
