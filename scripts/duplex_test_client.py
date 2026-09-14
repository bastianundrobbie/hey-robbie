#!/usr/bin/env python3
"""Test client for /voice/duplex — replays WAVs as the Pi's mic uplink.

Streams 16 kHz mono PCM16 WAVs in 100 ms binary chunks (real-time pacing by
default, --fast to blast), prints every JSON message the server sends, and
simulates the station: after every non-interrupted turn_end it reports
playback_done (so the server opens the follow-up window).

Scenario knobs:
    --wav W...        wake clip + command clip(s), sent immediately
    --followup W...   sent after the server's `listening` message arrives
                      (proves the follow-up window end-to-end)
    --barge W...      sent after the first TTS `audio` message arrives
                      (proves barge-in: expect abort_playback + interrupted
                      turn_end + a fresh turn)
    --wake-threshold  override the server's wakeword threshold (e.g. 0.0001
                      to push any speech clip through as a wake)

    # needs the websockets package (pip install websockets):
    python duplex_test_client.py --wav wake.wav
    python duplex_test_client.py --wav wake.wav cmd.wav --followup cmd2.wav
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import wave

from websockets.asyncio.client import connect

CHUNK_MS = 100
RATE = 16000
CHUNK_BYTES = RATE * 2 * CHUNK_MS // 1000  # 3200


def read_wav_pcm16(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, "need PCM16"
        assert w.getnchannels() == 1, "need mono"
        assert w.getframerate() == RATE, f"need {RATE} Hz (got {w.getframerate()})"
        return w.readframes(w.getnframes())


async def run(url: str, wav_paths: list[str], fast: bool, lead_silence: float,
              gap_silence: float, grace: float,
              wake_threshold: float | None = None,
              followup_paths: list[str] | None = None,
              barge_paths: list[str] | None = None) -> int:
    events: list[dict] = []
    listening_evt = asyncio.Event()
    first_audio_evt = asyncio.Event()
    got_audio = False

    async with connect(url, max_size=None, open_timeout=10) as ws:
        async def receiver():
            nonlocal got_audio
            async for msg in ws:
                if isinstance(msg, (bytes, bytearray)):
                    print(f"[recv] binary {len(msg)} bytes", file=sys.stderr)
                    continue
                data = json.loads(msg)
                events.append(data)
                print(f"[recv] {data}", file=sys.stderr)
                t = data.get("type")
                if t == "audio":
                    first_audio_evt.set()
                    got_audio = True
                elif t == "listening":
                    listening_evt.set()
                elif t == "turn_end" and not data.get("interrupted") and got_audio:
                    # Station simulation: pretend aplay drained instantly.
                    # (Like the real station, only turns that played audio
                    # report — a silent retry turn must not arm a stale flag.)
                    got_audio = False
                    await ws.send(json.dumps({"type": "playback_done"}))

        recv_task = asyncio.create_task(receiver())

        hello = {"type": "hello", "station": "test-client", "entity": "robbie"}
        if wake_threshold is not None:
            hello["wake_threshold"] = wake_threshold  # calibration/test knob
        await ws.send(json.dumps(hello))

        async def send_pcm(pcm: bytes) -> None:
            for i in range(0, len(pcm), CHUNK_BYTES):
                await ws.send(pcm[i:i + CHUNK_BYTES])
                if not fast:
                    await asyncio.sleep(CHUNK_MS / 1000)

        async def send_clips(paths: list[str]) -> None:
            for path in paths:
                pcm = read_wav_pcm16(path)
                print(f"[send] {path}: {len(pcm)} bytes ({len(pcm) / 32000:.1f}s)",
                      file=sys.stderr)
                await send_pcm(pcm)
                # Trailing silence after every clip — Deepgram's endpointing
                # needs actual silent *audio* to declare speech_final.
                await send_pcm(b"\x00" * int(gap_silence * RATE * 2))

        # Lead-in silence primes the ring buffer / detector like a real room.
        await send_pcm(b"\x00" * int(lead_silence * RATE * 2))
        await send_clips(wav_paths)

        if barge_paths:
            print("[test] waiting for first TTS audio to barge in...",
                  file=sys.stderr)
            await asyncio.wait_for(first_audio_evt.wait(), timeout=90)
            print("[test] BARGE NOW", file=sys.stderr)
            await send_clips(barge_paths)

        if followup_paths:
            print("[test] waiting for the follow-up window...", file=sys.stderr)
            await asyncio.wait_for(listening_evt.wait(), timeout=120)
            await send_clips(followup_paths)

        await asyncio.sleep(grace)
        recv_task.cancel()

    wakes = [e for e in events if e.get("type") == "wake_accepted"]
    print(f"\nRESULT: {len(wakes)} wake_accepted, events={[e.get('type') for e in events]}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="ws://127.0.0.1:8422/voice/duplex")
    ap.add_argument("--wav", nargs="+", required=True, help="16k mono PCM16 WAV(s)")
    ap.add_argument("--followup", nargs="+", default=None,
                    help="WAV(s) to send once the follow-up window opens")
    ap.add_argument("--barge", nargs="+", default=None,
                    help="WAV(s) to send as barge-in once TTS audio starts")
    ap.add_argument("--fast", action="store_true", help="no real-time pacing")
    ap.add_argument("--lead-silence", type=float, default=1.0)
    ap.add_argument("--gap-silence", type=float, default=2.0)
    ap.add_argument("--grace", type=float, default=2.0)
    ap.add_argument("--wake-threshold", type=float, default=None,
                    help="override the server's wakeword threshold (e.g. 0.01 "
                         "to push any speech clip through as a wake)")
    args = ap.parse_args()
    return asyncio.run(run(args.url, args.wav, args.fast, args.lead_silence,
                           args.gap_silence, args.grace, args.wake_threshold,
                           args.followup, args.barge))


if __name__ == "__main__":
    sys.exit(main())
