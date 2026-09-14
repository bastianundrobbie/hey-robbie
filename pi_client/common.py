#!/usr/bin/env python3
"""Shared helpers for the Pi speech-station clients (station + test clients).

Audio I/O is arecord/aplay subprocesses (no PortAudio, no numpy). `audioop`
is gone in Python 3.13, so the 24 kHz mono -> 48 kHz stereo conversion is
hand-rolled over `array` — 48/24 = 2 and 48/16 = 3 are integer, so
sample-and-hold is exact in time and only doubles channels.

The speakerphone is addressed by ALSA *name* (`plughw:CARD=USB`), not index —
the card index can wander across boots.

Settings come from the environment (set them in the systemd unit):
    ROBBIE_SERVER_HOST  hostname or IP of the server   (default below)
    ROBBIE_SERVER_PORT  server port                     (default 8422)
    ROBBIE_ALSA_DEV     ALSA device name                (default plughw:CARD=USB)
"""

import array
import json
import math
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import uuid

from websockets.sync.client import connect

# ── Server address ──────────────────────────────────────────────────────────
# PLACEHOLDER: hostname or IP of the machine running the server container.
SERVER_HOST = os.environ.get("ROBBIE_SERVER_HOST", "robbie-server.local")
SERVER_PORT = os.environ.get("ROBBIE_SERVER_PORT", "8422")
HTTP_BASE = f"http://{SERVER_HOST}:{SERVER_PORT}"
WS_BASE = f"ws://{SERVER_HOST}:{SERVER_PORT}"

STT_MODEL = "nova-3"
STT_LANGUAGE = "de"
# Silence (ms) Deepgram waits before declaring the turn over (speech_final).
# Deepgram's default (~10ms) fires on the briefest pause and clips speakers
# mid-thought; keep it patient so a thinking pause doesn't end the turn.
ENDPOINTING_MS = 1000
ENTITY = "robbie"
# Words Deepgram should get right (only used by the legacy test clients that
# call the STT proxy themselves; the duplex station leaves STT to the server).
KEYTERMS = ["Robbie", "Claude", "Anthropic"]

# ALSA device (by name — index can move between boots)
ALSA_DEV = os.environ.get("ROBBIE_ALSA_DEV", "plughw:CARD=USB")
CAPTURE_RATE = 16000   # native speakerphone capture rate = STT format, no resampling
PLAYBACK_RATE = 48000  # speakerphone playback hardware is fixed 48 kHz stereo


# ── Audio helpers ───────────────────────────────────────────────────────────

def beep(dev: str, freq: int = 880, ms: int = 180) -> None:
    """Play a short sine cue so the speaker knows recording has started."""
    n = PLAYBACK_RATE * ms // 1000
    buf = array.array("h")
    amp = 9000
    for i in range(n):
        s = int(amp * math.sin(2 * math.pi * freq * i / PLAYBACK_RATE))
        buf.append(s)  # L
        buf.append(s)  # R
    aplay(dev, buf.tobytes())


def record(dev: str, seconds: float) -> bytes:
    """Capture raw PCM16 mono 16 kHz from the Jabra via arecord (fixed duration)."""
    cmd = [
        "arecord", "-D", dev,
        "-f", "S16_LE", "-r", str(CAPTURE_RATE), "-c", "1",
        "-d", str(int(round(seconds))), "-t", "raw", "-q",
    ]
    print(f"[rec] {cmd}", file=sys.stderr)
    out = subprocess.run(cmd, capture_output=True)
    if out.returncode != 0:
        sys.exit(f"[rec] arecord failed ({out.returncode}): {out.stderr.decode(errors='replace')}")
    print(f"[rec] captured {len(out.stdout)} bytes "
          f"({len(out.stdout) / (CAPTURE_RATE * 2):.1f}s)", file=sys.stderr)
    return out.stdout


def aplay(dev: str, pcm48_stereo: bytes) -> None:
    """Play raw PCM16 48 kHz stereo via aplay (stdin)."""
    cmd = [
        "aplay", "-D", dev,
        "-f", "S16_LE", "-r", str(PLAYBACK_RATE), "-c", "2", "-t", "raw", "-q",
    ]
    out = subprocess.run(cmd, input=pcm48_stereo, capture_output=True)
    if out.returncode != 0:
        sys.exit(f"[play] aplay failed ({out.returncode}): {out.stderr.decode(errors='replace')}")


def to_48k_stereo(pcm: bytes, src_rate: int) -> bytes:
    """Upsample mono PCM16 to 48 kHz stereo by integer sample-and-hold.

    48/24 = 2 and 48/16 = 3 are exact, so each input sample is repeated
    `factor` times (time) and emitted on both channels (L=R). No audioop.
    """
    factor = max(1, round(PLAYBACK_RATE / src_rate))
    src = array.array("h")
    src.frombytes(pcm[: len(pcm) // 2 * 2])  # guard odd trailing byte
    # factor time-copies * 2 channels int16 per input sample
    out = array.array("h", bytes(len(src) * factor * 2 * 2))
    j = 0
    for s in src:
        for _ in range(factor):
            out[j] = s
            out[j + 1] = s
            j += 2
    return out.tobytes()


# ── STT (batch) ─────────────────────────────────────────────────────────────

def transcribe(http_base: str, pcm16: bytes) -> str:
    """POST raw PCM16 to /api/stt, return the transcript string."""
    params = urllib.parse.urlencode({
        "model": STT_MODEL,
        "language": STT_LANGUAGE,
        "smart_format": "true",
        "keywords": ",".join(KEYTERMS),
    })
    url = f"{http_base}/api/stt?{params}"
    req = urllib.request.Request(
        url, data=pcm16, method="POST",
        headers={"Content-Type": "audio/raw"},
    )
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read()
    data = json.loads(body)
    transcript = (data.get("transcript") or "").strip()
    print(f"[stt] \"{transcript}\" "
          f"(server={data.get('duration_ms', 0)}ms, total={int((time.monotonic()-t0)*1000)}ms)",
          file=sys.stderr)
    return transcript


# ── Voice turn over WS (/voice/stream) ──────────────────────────────────────

def run_turn(ws_base: str, transcript: str,
             speaker: str = "") -> tuple[list[str], bytes, int]:
    """Send one turn, collect sentences + all PCM until turn_end.

    Returns (sentences, combined_pcm, sample_rate). Handles both the streaming
    (audio streaming=true + frames + audio_end) and legacy (single frame after
    audio meta) shapes by simply concatenating every binary frame of the turn.
    An optional speaker label is passed through to the server (voice-ID).
    """
    turn_id = uuid.uuid4().hex
    url = f"{ws_base}/voice/stream"
    sentences: list[str] = []
    pcm_chunks: list[bytes] = []
    sample_rate = 24000

    print(f"[ws] connecting {url}", file=sys.stderr)
    with connect(url, open_timeout=10, max_size=None) as ws:
        ws.send(json.dumps({"type": "entity_wake", "entity": ENTITY}))
        msg = {
            "type": "turn_start",
            "turn_id": turn_id,
            "transcript": transcript,
            "channel": "voice",
            "entity": ENTITY,
        }
        if speaker:
            msg["speaker"] = speaker
        ws.send(json.dumps(msg))

        while True:
            try:
                msg = ws.recv(timeout=60)
            except TimeoutError:
                print("[ws] recv timeout — giving up", file=sys.stderr)
                break

            if isinstance(msg, (bytes, bytearray)):
                pcm_chunks.append(bytes(msg))
                continue

            data = json.loads(msg)
            mtype = data.get("type")
            if mtype == "turn_ack":
                print(f"[ws] turn_ack {data.get('turn_id')}", file=sys.stderr)
            elif mtype == "sentence":
                text = data.get("text", "")
                if text:
                    sentences.append(text)
                    print(f"[robbie] {text}")
            elif mtype == "audio":
                sample_rate = data.get("sample_rate", sample_rate)
            elif mtype == "audio_end":
                pass
            elif mtype == "turn_end":
                print(f"[ws] turn_end (interrupted={data.get('interrupted')})", file=sys.stderr)
                break
            elif mtype == "error":
                print(f"[ws] ERROR: {data.get('message')}", file=sys.stderr)
                break

        try:
            ws.send(json.dumps({"type": "entity_sleep", "entity": ENTITY}))
        except Exception:
            pass

    combined = b"".join(pcm_chunks)
    total_s = len(combined) / (sample_rate * 2) if sample_rate else 0
    print(f"[ws] collected {len(sentences)} sentences, {len(combined)} bytes PCM "
          f"@ {sample_rate} Hz ({total_s:.1f}s)", file=sys.stderr)
    return sentences, combined, sample_rate


def play_response(dev: str, pcm: bytes, rate: int) -> None:
    """Convert a mono TTS PCM buffer to 48 kHz stereo and play it."""
    print(f"[play] converting {rate} Hz mono -> {PLAYBACK_RATE} Hz stereo", file=sys.stderr)
    aplay(dev, to_48k_stereo(pcm, rate))


# ── Streaming playback (time-to-first-audio path) ────────────────────────────

class StreamPlayer:
    """Incremental playback through one persistent aplay process.

    write() converts mono TTS PCM to 48 kHz stereo and pipes it to aplay as it
    arrives, so the first sentence starts playing while the server is still
    generating the rest. Writes block once aplay's buffer is full — that's the
    pacing; WS frames simply queue up meanwhile. close() sends EOF and waits
    for aplay to drain (so the mic never reopens over our own tail audio).
    """

    def __init__(self, dev: str):
        self._proc = subprocess.Popen(
            ["aplay", "-D", dev, "-f", "S16_LE", "-r", str(PLAYBACK_RATE),
             "-c", "2", "-t", "raw", "-q"],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # inter-sentence gaps cause underrun spam
        )
        self._carry = b""  # odd trailing byte if a frame splits an int16 sample

    def write(self, pcm: bytes, src_rate: int) -> None:
        buf = self._carry + pcm
        if len(buf) % 2:
            buf, self._carry = buf[:-1], buf[-1:]
        else:
            self._carry = b""
        if buf and self._proc.stdin:
            self._proc.stdin.write(to_48k_stereo(buf, src_rate))

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()  # EOF -> aplay drains and exits
        except Exception:
            pass
        self._proc.wait()


def run_turn_streaming(ws_base: str, transcript: str, dev: str,
                       speaker: str = "") -> tuple[list[str], int, int]:
    """Like run_turn, but plays audio as it arrives instead of after turn_end.

    Cuts time-to-first-audio to "first sentence ready" instead of "whole reply
    ready" — the win grows with reply length. Returns (sentences,
    pcm_bytes_played, sample_rate). An optional speaker label is passed
    through to the server (voice-ID).
    """
    turn_id = uuid.uuid4().hex
    url = f"{ws_base}/voice/stream"
    sentences: list[str] = []
    played = 0
    sample_rate = 24000
    player: StreamPlayer | None = None
    t0 = time.monotonic()

    print(f"[ws] connecting {url}", file=sys.stderr)
    try:
        with connect(url, open_timeout=10, max_size=None) as ws:
            ws.send(json.dumps({"type": "entity_wake", "entity": ENTITY}))
            msg_start = {
                "type": "turn_start",
                "turn_id": turn_id,
                "transcript": transcript,
                "channel": "voice",
                "entity": ENTITY,
            }
            if speaker:
                msg_start["speaker"] = speaker
            ws.send(json.dumps(msg_start))

            while True:
                try:
                    msg = ws.recv(timeout=60)
                except TimeoutError:
                    print("[ws] recv timeout — giving up", file=sys.stderr)
                    break

                if isinstance(msg, (bytes, bytearray)):
                    if player is None:
                        player = StreamPlayer(dev)
                        print(f"[play] first audio after "
                              f"{int((time.monotonic() - t0) * 1000)} ms (streaming)",
                              file=sys.stderr)
                    player.write(bytes(msg), sample_rate)
                    played += len(msg)
                    continue

                data = json.loads(msg)
                mtype = data.get("type")
                if mtype == "turn_ack":
                    print(f"[ws] turn_ack {data.get('turn_id')}", file=sys.stderr)
                elif mtype == "sentence":
                    text = data.get("text", "")
                    if text:
                        sentences.append(text)
                        print(f"[robbie] {text}")
                elif mtype == "audio":
                    sample_rate = data.get("sample_rate", sample_rate)
                elif mtype == "audio_end":
                    pass
                elif mtype == "turn_end":
                    print(f"[ws] turn_end (interrupted={data.get('interrupted')})",
                          file=sys.stderr)
                    break
                elif mtype == "error":
                    print(f"[ws] ERROR: {data.get('message')}", file=sys.stderr)
                    break

            try:
                ws.send(json.dumps({"type": "entity_sleep", "entity": ENTITY}))
            except Exception:
                pass
    finally:
        if player is not None:
            player.close()  # drain before the caller reopens the mic

    total_s = played / (sample_rate * 2) if sample_rate else 0
    print(f"[ws] streamed {len(sentences)} sentences, {played} bytes PCM "
          f"@ {sample_rate} Hz ({total_s:.1f}s)", file=sys.stderr)
    return sentences, played, sample_rate
