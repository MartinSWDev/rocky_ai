#!/usr/bin/env python3
"""Rocky voice client — runs on the Mac.

Records the mic -> Whisper STT -> sends the query to the PC over Tailscale ->
speaks Rocky's reply with rocky_say (Coqui XTTS). Two modes:

  python3 rocky_client.py            text mode (type questions)
  python3 rocky_client.py --voice    voice mode (wake word "Hey Rocky")

All paths/IPs come from config.yaml (see config.example.yaml).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

from config import expand, load_config

CFG = load_config()

PC_IP        = CFG.get("network.pc_tailscale_ip", "100.x.y.z")
PC_PORT      = CFG.get("network.server_port", 7437)
REQ_TIMEOUT  = CFG.get("ollama.request_timeout", 120)

RECORD_SECS  = CFG.get("voice.record_secs", 10)
SILENCE_DB   = CFG.get("voice.silence_db", -30)
SILENCE_SECS = CFG.get("voice.silence_secs", 1.5)
WHISPER_BIN  = expand(CFG.get("voice.whisper_bin", "/opt/homebrew/bin/whisper-cli"))
WHISPER_MODEL      = expand(CFG.get("voice.whisper_model", "~/.whisper/ggml-base.en.bin"))
WHISPER_WAKE_MODEL = expand(CFG.get("voice.whisper_wake_model", "~/.whisper/ggml-tiny.en.bin"))
WAKE_WORDS   = [w.lower() for w in CFG.get("voice.wake_words", ["hey rocky"])]

TTS_PORT     = CFG.get("voice.tts_port", 59720)
TTS_REF      = expand(CFG.get("voice.tts_reference", "~/.rocky_say/rocky_training_audio_scrubbed.wav"))
TTS_PYTHON   = expand(CFG.get("voice.tts_venv_python", "~/.rocky_say/venv/bin/python3"))

USER_NAME    = CFG.get("rocky.user_name", "User")
# Optional shared secret matching the server's ROCKY_AUTH_TOKEN.
AUTH_TOKEN   = (os.environ.get("ROCKY_AUTH_TOKEN") or CFG.get("network.auth_token") or "")


def check_server() -> bool:
    try:
        resp = urllib.request.urlopen(f"http://{PC_IP}:{PC_PORT}/health", timeout=3)
        return json.loads(resp.read()).get("status") == "ok"
    except Exception:
        return False


def record_audio(seconds=RECORD_SECS) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
    tmp.close()
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", str(seconds), "-ar", "16000", "-ac", "1", tmp.name,
    ], capture_output=True)
    return tmp.name


def transcribe(wav_path: str, model: str | None = None) -> str:
    model = model or WHISPER_MODEL
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


def ask_rocky(query: str) -> str:
    payload = json.dumps({"query": query}).encode()
    headers = {"Content-Type": "application/json"}
    if AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {AUTH_TOKEN}"
    req = urllib.request.Request(
        f"http://{PC_IP}:{PC_PORT}",
        data=payload,
        headers=headers,
    )
    resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
    return json.loads(resp.read()).get("response", "")


def speak(text: str) -> None:
    # Fast path: rocky_say persistent server (~3s).
    try:
        payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{TTS_PORT}",
            data=payload,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=REQ_TIMEOUT)
        wav = resp.read()
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(wav)
            tmp = f.name
        subprocess.run(["afplay", tmp])
        os.unlink(tmp)
        return
    except Exception as exc:
        print(f"TTS server failed: {exc}, falling back to standalone")

    # Slow fallback: spawn the venv python and load the model fresh (~20s).
    # The reply text is read from a file inside the subprocess — never
    # interpolated into the script — so a crafted response can't inject code.
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False, mode="w", encoding="utf-8") as tf:
        tf.write(text)
        text_file = tf.name
    script = f"""
import os
os.environ["KMP_DUPLICATE_LIB_OK"] = "TRUE"
os.environ["OMP_NUM_THREADS"] = "1"
from TTS.api import TTS
text = open({text_file!r}, encoding="utf-8").read()
tts = TTS("tts_models/multilingual/multi-dataset/xtts_v2")
tts.tts_to_file(text=text, speaker_wav={TTS_REF!r}, language="en", file_path={tmp!r})
"""
    subprocess.run([TTS_PYTHON, "-c", script], capture_output=True)
    os.unlink(text_file)
    subprocess.run(["afplay", tmp])
    os.unlink(tmp)


def listen_for_wake_word() -> bool:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", "3", "-ar", "16000", "-ac", "1", tmp,
    ], capture_output=True)
    result = subprocess.run([
        WHISPER_BIN, "--model", WHISPER_WAKE_MODEL,
        "--file", tmp, "--no-timestamps",
    ], capture_output=True, text=True)
    os.unlink(tmp)
    transcript = result.stdout.lower()
    return any(phrase in transcript for phrase in WAKE_WORDS)


def record_until_silence(max_secs=10) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        tmp = f.name
    subprocess.run([
        "ffmpeg", "-y", "-f", "avfoundation", "-i", ":0",
        "-t", str(max_secs), "-ar", "16000", "-ac", "1",
        "-af", f"silencedetect=noise={SILENCE_DB}dB:d={SILENCE_SECS}", tmp,
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
                print("Rocky thinking...")
                t0 = time.time()
                response = ask_rocky(query)
                print(f"Rocky ({time.time() - t0:.1f}s): {response}")
                speak(response)
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
                print("Rocky thinking...")
                t0 = time.time()
                response = ask_rocky(query)
                print(f"Rocky ({time.time() - t0:.1f}s): {response}")
                speak(response)
            except KeyboardInterrupt:
                print("\nStopped.")
                break


if __name__ == "__main__":
    main()
