#!/usr/bin/env python3
"""Robbie duplex station — the Pi as a pure microphone.

One persistent WebSocket to the server's /voice/duplex carries everything:
the mic uplink (continuous arecord 16 kHz mono, 100 ms binary chunks) and the
downlink (control messages + streaming TTS audio). Wakeword, voice-ID gate,
STT and barge-in all run server-side now — this client only moves audio and
plays cues:

    binary out   mic PCM16 16 kHz (never stops; Jabra AEC keeps it echo-free
                 during playback, so the server can hear "Hey Robbie" barge-ins)
    wake_accepted    -> go-ahead beep (speak now)
    turn_transcript  -> log
    sentence         -> log
    audio + binary   -> streaming playback (24 kHz mono -> 48 kHz stereo)
    turn_end         -> drain playback, then report playback_done
    listening        -> follow-up window cue (higher tone, just talk)
    notify           -> chime (timer/alarm announcement follows as a turn)
    alarm_ring       -> loop the alarm tone until alarm_ring_stop arrives
    alarm_ring_stop  -> stop the tone (joined before any further playback,
                        so the tone never overlaps the TTS player)
    abort_playback   -> kill playback immediately (server-side barge-in)
    turn_aborted     -> log (no tone for no_speech; that's a normal outcome;
                        soft low cue for reason "stop" = "Robbie stopp" heard)
    error            -> low error tone

Server gone -> reconnect with backoff + error tone (the station is useless
without the server anyway; same stance as the old app.py loop).

    python station.py                  # run forever (appliance mode)
    python station.py --once           # exit after the first disconnect (test)
"""

import argparse
import array
import asyncio
import json
import math
import queue
import subprocess
import sys
import threading
import time

from websockets.asyncio.client import connect

import common

CHUNK_BYTES = 3200  # 100 ms of PCM16 mono @ 16 kHz
RECONNECT_MIN_S = 2.0
RECONNECT_MAX_S = 30.0

# Alarm ring tone: three short beeps + a pause per burst, looped until the
# server says stop (or the connection dies).
RING_FREQ = 950
RING_BEEP_MS = 150
RING_GAP_MS = 80
RING_PAUSE_MS = 700


def _ring_burst_pcm() -> bytes:
    """One alarm burst (beep-beep-beep … pause) as 48 kHz stereo PCM16."""
    rate = common.PLAYBACK_RATE
    amp = 12000

    def tone(ms: int) -> array.array:
        n = rate * ms // 1000
        buf = array.array("h")
        for i in range(n):
            s = int(amp * math.sin(2 * math.pi * RING_FREQ * i / rate))
            buf.append(s)  # L
            buf.append(s)  # R
        return buf

    def silence(ms: int) -> array.array:
        return array.array("h", bytes(4 * (rate * ms // 1000)))

    out = array.array("h")
    for _ in range(3):
        out += tone(RING_BEEP_MS)
        out += silence(RING_GAP_MS)
    out += silence(RING_PAUSE_MS)
    return out.tobytes()


class Ringer:
    """Looping alarm tone via short-lived aplay calls (one burst each).

    Runs in a thread so the receiver stays responsive. stop() sets the event
    and joins — afterwards the playback device is guaranteed free again. The
    server sequences tone and TTS strictly (announce -> tone -> stop -> reply);
    this join is the local half of that no-overlap contract. Deliberately not
    common.aplay (it sys.exit()s on failure — fatal inside a thread); a failed
    burst just retries after a short sleep.
    """

    def __init__(self, dev: str):
        self._dev = dev
        self._stop = threading.Event()
        self._burst = _ring_burst_pcm()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                subprocess.run(
                    ["aplay", "-D", self._dev, "-f", "S16_LE",
                     "-r", str(common.PLAYBACK_RATE), "-c", "2", "-t", "raw",
                     "-q"],
                    input=self._burst,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            except Exception:
                time.sleep(0.5)

    def stop(self) -> None:
        """Stop the tone; returns once the device is free (<= one burst)."""
        self._stop.set()
        self._thread.join(timeout=5)


class Player:
    """Streaming TTS playback through one aplay, fed from a writer thread.

    The receiver loop must never block (abort_playback has to be handled
    mid-playback), so blocking aplay writes happen in a thread; the queue is
    the hand-off. close() drains (EOF -> aplay finishes the tail); abort()
    kills aplay instantly and discards whatever is queued.
    """

    def __init__(self, dev: str):
        self._dev = dev
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._aborted = False
        self._proc = subprocess.Popen(
            ["aplay", "-D", dev, "-f", "S16_LE", "-r", str(common.PLAYBACK_RATE),
             "-c", "2", "-t", "raw", "-q"],
            stdin=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._carry = b""
        self._thread = threading.Thread(target=self._writer, daemon=True)
        self._thread.start()

    def _writer(self) -> None:
        while True:
            item = self._q.get()
            if item is None or self._aborted:
                break
            try:
                if self._proc.stdin:
                    self._proc.stdin.write(item)
            except (BrokenPipeError, OSError):
                break
        try:
            if self._proc.stdin and not self._aborted:
                self._proc.stdin.close()  # EOF -> aplay drains and exits
        except Exception:
            pass

    def write(self, pcm: bytes, src_rate: int) -> None:
        """Queue a mono TTS chunk (converted to 48 kHz stereo)."""
        buf = self._carry + pcm
        if len(buf) % 2:
            buf, self._carry = buf[:-1], buf[-1:]
        else:
            self._carry = b""
        if buf:
            self._q.put(common.to_48k_stereo(buf, src_rate))

    def close(self) -> None:
        """Signal end of turn, wait for aplay to drain the tail."""
        self._q.put(None)
        self._thread.join(timeout=60)
        try:
            self._proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            self._proc.terminate()

    def abort(self) -> None:
        """Barge-in: stop the voice NOW."""
        self._aborted = True
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        self._q.put(None)  # unblock the writer thread
        try:
            self._proc.terminate()
            self._proc.wait(timeout=2)
        except Exception:
            pass


async def mic_sender(ws, dev: str) -> None:
    """Continuous arecord -> 100 ms binary chunks. Runs until the WS drops."""
    proc = await asyncio.create_subprocess_exec(
        "arecord", "-D", dev, "-f", "S16_LE", "-r", str(common.CAPTURE_RATE),
        "-c", "1", "-t", "raw", "-q",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        while True:
            chunk = await proc.stdout.read(CHUNK_BYTES)
            if not chunk:
                raise RuntimeError("arecord stream ended")
            await ws.send(chunk)
    finally:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        except Exception:
            pass


async def receiver(ws, dev: str) -> None:
    """Handle control messages + streaming TTS audio from the server."""
    player: Player | None = None
    ringer: Ringer | None = None
    sample_rate = 24000
    turn_t0 = 0.0

    async for msg in ws:
        if isinstance(msg, (bytes, bytearray)):
            if player is None:
                player = Player(dev)
                if turn_t0:
                    print(f"[station] first audio after "
                          f"{int((time.monotonic() - turn_t0) * 1000)} ms",
                          file=sys.stderr)
            player.write(bytes(msg), sample_rate)
            continue

        data = json.loads(msg)
        mtype = data.get("type")

        if mtype == "hello_ack":
            print(f"[station] connected (entity={data.get('entity')})",
                  file=sys.stderr)
            common.beep(dev, freq=440, ms=90)  # ready cue
        elif mtype == "wake_accepted":
            turn_t0 = time.monotonic()
            print(f"[station] wake accepted: speaker={data.get('speaker')} "
                  f"(wake={data.get('score')}, gate={data.get('gate_score')})",
                  file=sys.stderr)
            common.beep(dev, freq=1200, ms=120)  # go-ahead cue
        elif mtype == "turn_transcript":
            print(f"[you] {data.get('text', '')}", file=sys.stderr)
        elif mtype == "sentence":
            text = data.get("text", "")
            if text:
                print(f"[robbie] {text}")
        elif mtype == "audio":
            sample_rate = data.get("sample_rate", sample_rate)
        elif mtype == "audio_end":
            pass
        elif mtype == "turn_end":
            print(f"[station] turn_end (interrupted={data.get('interrupted')})",
                  file=sys.stderr)
            played = player is not None
            if player is not None:
                await asyncio.to_thread(player.close)
                player = None
            turn_t0 = 0.0
            # Report the drained playback — the server opens the follow-up
            # mic only after this (otherwise it would transcribe Robbie).
            # Only when audio actually played: a turn_end without audio (the
            # server's silent retry) must not arm a stale report.
            if played and not data.get("interrupted"):
                await ws.send(json.dumps({"type": "playback_done"}))
        elif mtype == "listening":
            print(f"[station] follow-up window ({data.get('window')}s) — just talk",
                  file=sys.stderr)
            common.beep(dev, freq=1500, ms=100)  # higher tone: no wakeword needed
        elif mtype == "notify":
            print(f"[station] notification ({data.get('source')})", file=sys.stderr)
            common.beep(dev, freq=880, ms=180)  # chime before the announcement
        elif mtype == "alarm_ring":
            print("[station] alarm ringing", file=sys.stderr)
            if ringer is None:
                ringer = Ringer(dev)
        elif mtype == "alarm_ring_stop":
            print("[station] alarm ring stop", file=sys.stderr)
            if ringer is not None:
                # to_thread: this coroutine (and thus message handling) waits
                # for the join — no aplay overlap with the TTS that follows —
                # while the mic uplink task keeps running.
                await asyncio.to_thread(ringer.stop)
                ringer = None
        elif mtype == "abort_playback":
            print("[station] abort_playback (barge-in)", file=sys.stderr)
            if player is not None:
                player.abort()
                player = None
        elif mtype == "turn_aborted":
            reason = data.get("reason")
            print(f"[station] turn aborted: {reason}", file=sys.stderr)
            if reason == "stop":
                # "Robbie stopp" acknowledged — short low cue, not an error.
                common.beep(dev, freq=330, ms=150)
        elif mtype == "error":
            print(f"[station] server error: {data.get('message')}", file=sys.stderr)
            try:
                common.beep(dev, freq=220, ms=250)
            except Exception:
                pass
        elif mtype == "pong":
            pass
        else:
            print(f"[station] unknown message type: {mtype}", file=sys.stderr)

    if player is not None:
        player.abort()
    if ringer is not None:
        ringer.stop()  # server gone -> no endless ringing


async def run_once(ws_base: str, dev: str, station: str) -> None:
    url = f"{ws_base}/voice/duplex"
    print(f"[station] connecting {url}", file=sys.stderr)
    async with connect(url, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps(
            {"type": "hello", "station": station, "entity": common.ENTITY}))
        send_task = asyncio.create_task(mic_sender(ws, dev))
        recv_task = asyncio.create_task(receiver(ws, dev))
        done, pending = await asyncio.wait(
            {send_task, recv_task}, return_when=asyncio.FIRST_EXCEPTION)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc:
                raise exc


def main() -> None:
    ap = argparse.ArgumentParser(description="Robbie duplex station")
    ap.add_argument("--ws", default=common.WS_BASE, help="server WS base")
    ap.add_argument("--dev", default=common.ALSA_DEV, help="ALSA device name")
    ap.add_argument("--station", default="pi", help="station name in hello")
    ap.add_argument("--once", action="store_true",
                    help="exit after the first disconnect (test mode)")
    args = ap.parse_args()

    backoff = RECONNECT_MIN_S
    while True:
        t_start = time.monotonic()
        try:
            asyncio.run(run_once(args.ws, args.dev, args.station))
            print("[station] connection closed", file=sys.stderr)
        except KeyboardInterrupt:
            print("\n[station] bye", file=sys.stderr)
            return
        except Exception as e:
            print(f"[station] connection failed: {type(e).__name__}: {e}",
                  file=sys.stderr)
            try:
                common.beep(args.dev, freq=220, ms=250)
            except Exception:
                pass

        if args.once:
            return
        # A connection that lived a while resets the backoff.
        if time.monotonic() - t_start > 60:
            backoff = RECONNECT_MIN_S
        print(f"[station] reconnecting in {backoff:.0f}s", file=sys.stderr)
        time.sleep(backoff)
        backoff = min(backoff * 2, RECONNECT_MAX_S)


if __name__ == "__main__":
    main()
