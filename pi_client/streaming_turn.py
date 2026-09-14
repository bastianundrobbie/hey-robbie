#!/usr/bin/env python3
"""Streaming-STT voice turn for the Pi 3 + Jabra speech station (bring-up step 3).

Unlike batch_turn.py (fixed record duration), this streams mic PCM live to the
`/api/stt/stream` proxy and ends the turn on **semantic VAD** — Deepgram's
`speech_final` — so you just speak and stop; no fixed timer:

    connect STT WS -> beep -> arecord streams 100 ms PCM chunks
      -> receive Deepgram Results -> end on speech_final (with text)
      -> CloseStream -> transcript -> WS /voice/stream turn -> 48 kHz stereo -> aplay

Concurrency: streaming needs to send audio and receive transcripts at the same
time, so the STT leg uses asyncio (two tasks: sender + receiver). The subsequent
/voice/stream turn reuses the synchronous common.run_turn.

Wire format (verified against the live server): `/api/stt/stream` proxies
   **raw Deepgram** messages. There is no {"type":"ready"} and audio must flow
   before anything comes back (Deepgram upstream connects lazily, ~1.3 s to first
   Results). Messages:
     {"type":"Results", "is_final":bool, "speech_final":bool,
      "channel":{"alternatives":[{"transcript":"..."}]}}   — interim/final text
     {"type":"UtteranceEnd", ...}      — utterance boundary (if enabled)
     {"type":"SpeechStarted", ...}     — VAD speech onset (if enabled)
     {"type":"Metadata", ...}          — end-of-stream summary after CloseStream
   Client -> server: binary PCM16 chunks; {"type":"CloseStream"} to finalize.
"""

import argparse
import asyncio
import json
import sys
import urllib.parse

from websockets.asyncio.client import connect

import common

CHUNK_BYTES = 3200  # 100 ms of PCM16 mono @ 16 kHz


def _deepgram_text(data: dict) -> str:
    alts = (data.get("channel") or {}).get("alternatives") or [{}]
    return (alts[0].get("transcript") or "").strip()


async def stream_transcribe(
    ws_base: str,
    dev: str,
    *,
    do_beep: bool = True,
    no_speech_timeout: float = 8.0,
    max_seconds: float = 20.0,
) -> tuple[str, str | None]:
    """Stream mic audio to /api/stt/stream, return (transcript, error)."""
    url = f"{ws_base}/api/stt/stream?" + urllib.parse.urlencode({
        "model": common.STT_MODEL,
        "language": common.STT_LANGUAGE,
        "smart_format": "true",
        "keywords": ",".join(common.KEYTERMS),
        "endpointing": str(common.ENDPOINTING_MS),
    })

    final_segments: list[str] = []
    interim = ""
    err: str | None = None
    got_speech = asyncio.Event()
    done = asyncio.Event()

    async def receiver(ws) -> None:
        nonlocal interim, err
        async for msg in ws:
            if isinstance(msg, (bytes, bytearray)):
                continue
            data = json.loads(msg)
            t = data.get("type")
            if t == "Results":
                text = _deepgram_text(data)
                if text:
                    got_speech.set()
                    if data.get("is_final"):
                        final_segments.append(text)
                        interim = ""
                        print(f"[stt] final: \"{text}\"", file=sys.stderr)
                    else:
                        interim = text
                        print(f"[stt] interim: \"{text}\"", file=sys.stderr)
                # speech_final = Deepgram endpoint; only end if we actually have text
                if data.get("speech_final") and (final_segments or interim):
                    if interim:
                        final_segments.append(interim)
                        interim = ""
                    print("[stt] speech_final -> end of turn", file=sys.stderr)
                    done.set()
                    return
            elif t == "UtteranceEnd":
                if interim:
                    final_segments.append(interim)
                    interim = ""
                if final_segments:
                    print("[stt] utterance_end -> end of turn", file=sys.stderr)
                    done.set()
                    return
            elif t == "SpeechStarted":
                print("[stt] speech_started", file=sys.stderr)
                got_speech.set()
            elif t in ("error", "Error"):
                err = data.get("message") or data.get("description") or "unknown STT error"
                print(f"[stt] ERROR: {err}", file=sys.stderr)
                done.set()
                return
            # Metadata and anything else: ignore

    async def sender(ws, proc) -> None:
        try:
            while not done.is_set():
                chunk = await proc.stdout.read(CHUNK_BYTES)
                if not chunk:
                    break
                await ws.send(chunk)
        except Exception as e:  # noqa: BLE001 — sender is best-effort
            print(f"[stt] sender stopped: {e}", file=sys.stderr)

    async with connect(url, max_size=None, open_timeout=10) as ws:
        recv_task = asyncio.create_task(receiver(ws))

        # No "ready" from this server — beep, then stream immediately.
        if do_beep:
            common.beep(dev)
        print("[stt] streaming (speak after the beep, then pause)", file=sys.stderr)

        proc = await asyncio.create_subprocess_exec(
            "arecord", "-D", dev, "-f", "S16_LE", "-r", str(common.CAPTURE_RATE),
            "-c", "1", "-t", "raw", "-q",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        send_task = asyncio.create_task(sender(ws, proc))

        # Wait for speech to start, then for the semantic end, with a hard cap.
        try:
            await asyncio.wait_for(got_speech.wait(), timeout=no_speech_timeout)
        except TimeoutError:
            print("[stt] no speech detected", file=sys.stderr)
            done.set()
        else:
            try:
                await asyncio.wait_for(done.wait(), timeout=max_seconds)
            except TimeoutError:
                print("[stt] max turn cap reached", file=sys.stderr)
                done.set()

        # Finalize: CloseStream, brief grace for a trailing speech_final.
        try:
            await ws.send(json.dumps({"type": "CloseStream"}))
        except Exception:
            pass
        try:
            await asyncio.wait_for(done.wait(), timeout=0.8)
        except TimeoutError:
            pass

        send_task.cancel()
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)  # reap arecord (no <defunct>)
        except Exception:
            pass
        recv_task.cancel()

    transcript = " ".join(final_segments + ([interim] if interim else [])).strip()
    return transcript, err


def main() -> None:
    ap = argparse.ArgumentParser(description="Pi streaming-STT voice turn")
    ap.add_argument("--http", default=common.HTTP_BASE, help="server HTTP base")
    ap.add_argument("--ws", default=common.WS_BASE, help="server WS base")
    ap.add_argument("--dev", default=common.ALSA_DEV, help="ALSA device name")
    ap.add_argument("--no-beep", action="store_true", help="skip the start cue")
    ap.add_argument("--no-speech", type=float, default=8.0,
                    help="abort if no speech within N seconds")
    ap.add_argument("--max-seconds", type=float, default=20.0,
                    help="hard cap on turn length")
    args = ap.parse_args()

    transcript, err = asyncio.run(stream_transcribe(
        args.ws, args.dev,
        do_beep=not args.no_beep,
        no_speech_timeout=args.no_speech,
        max_seconds=args.max_seconds,
    ))

    if err:
        sys.exit(f"[stt] streaming error: {err}")
    if not transcript:
        sys.exit("[stt] empty transcript — nothing heard")
    print(f"[stt] final transcript: \"{transcript}\"", file=sys.stderr)

    sentences, pcm, rate = common.run_turn(args.ws, transcript)
    if not pcm:
        print("[play] no audio received — check server logs (API key missing or invalid?)",
              file=sys.stderr)
        sys.exit(1)

    common.play_response(args.dev, pcm, rate)
    print("[done]", file=sys.stderr)


if __name__ == "__main__":
    main()
