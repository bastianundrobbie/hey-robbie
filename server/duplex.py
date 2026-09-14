"""Persistent duplex audio link — the Pi as a pure microphone.

The Pi streams its 16 kHz mono capture continuously over ONE WebSocket and
everything heavy runs server-side: a volatile RAM ring buffer, openWakeWord,
per-turn Deepgram STT, barge-in and proactive notify turns. The Pi keeps only
arecord/aplay + beeps.

Speaker identification: this edition has none. The ``speaker`` field in the
messages below is kept (empty) so a voice-ID gate can be slotted in behind
the wakeword later without changing the station.

Protocol ``/voice/duplex`` (single persistent connection, singleton):

    Pi -> server:  binary frames  = raw PCM16 mono 16 kHz mic audio
                   {"type": "hello", "station": "...", "entity": "robbie"}
                   {"type": "playback_done"}   -> station's aplay drained
                   {"type": "ping"}
    server -> Pi:  {"type": "hello_ack", "entity": "..."}
                   {"type": "wake_accepted", "score": 0.87,
                    "speaker": ""}  -> go-ahead cue
                   {"type": "turn_transcript", "turn_id": "...",
                    "text": "...", "speaker": ""}
                   {"type": "turn_aborted", "reason": "no_speech"}
                    (reasons: no_speech / empty_answer / stop / STT error —
                     "stop" is the spoken "Robbie stopp" conversation kill)
                   {"type": "listening", "window": 6.0}  -> follow-up cue
                   {"type": "abort_playback"}  -> barge-in: kill aplay NOW
                   {"type": "notify", "source": "timer"}  -> chime, then a
                    spoken notification arrives as sentence/audio/turn_end
                   sentence / audio meta / binary TTS PCM 24 kHz / audio_end /
                   turn_end   (identical shapes to /voice/stream)
                   {"type": "pong"}
                   {"type": "error", "message": "..."}

Binary directions never collide: Pi->server binary is always mic PCM 16 kHz,
server->Pi binary is always TTS PCM 24 kHz.

Nothing from the mic stream is ever persisted: the ring buffer lives in RAM
only (~2 MB) and dies with the connection/process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections import deque
from pathlib import Path

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)

# ── Audio / buffer constants ────────────────────────────────────────────────
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_SECOND = SAMPLE_RATE * BYTES_PER_SAMPLE

RING_SECONDS = 60  # volatile pre-roll window (~1.9 MB)

# ── Wakeword (openWakeWord, same calibration as the Pi ran) ────────────────
OWW_FRAME_SAMPLES = 1280  # 80 ms — openWakeWord's chunk size
OWW_FRAME_BYTES = OWW_FRAME_SAMPLES * BYTES_PER_SAMPLE
WAKE_MODEL_PATH = os.environ.get(
    "ROBBIE_WAKE_MODEL",
    str(Path(__file__).resolve().parent.parent / "models" / "hey_robbie.onnx"),
)
WAKE_KEY = "hey_robbie"
# Real "Hey Robbie" scored ~0.5-0.97 on the reference speakerphone; noise
# stayed < 0.4. Calibrate for your room/mic via ROBBIE_WAKE_THRESHOLD.
WAKE_SCORE_THRESHOLD = float(os.environ.get("ROBBIE_WAKE_THRESHOLD", "0.4"))
NEAR_MISS_FLOOR = 0.25  # log elevated-but-sub-threshold bursts
# Barge-in needs a DELIBERATE wake, not a marginal one: cutting Robbie off
# mid-answer is destructive. The speaker's own flowing speech can score ~0.42
# and would kill a running answer, which then gets re-answered from the ring
# buffer ("Robbie answers old questions"). Real barge shouts score 0.8+.
BARGE_WAKE_THRESHOLD = float(os.environ.get("ROBBIE_BARGE_WAKE_THRESHOLD", "0.6"))
# Refractory is measured in AUDIO time (frames), not wall-clock: WS delivery
# can be bursty (WLAN jitter, test replays), and what must not re-trigger is
# the next stretch of the *signal*, regardless of when it arrives. Frames keep
# flowing through the model during refractory (scores discarded) so the wake
# phrase drains out of openWakeWord's feature window instead of re-triggering
# on its own tail; 2 s covers the phrase plus the model's context.
REFRACTORY_SECONDS = 2.0
REFRACTORY_FRAMES = int(REFRACTORY_SECONDS * SAMPLE_RATE / OWW_FRAME_SAMPLES)

# ── Per-turn STT (Deepgram — strictly per turn, never on the idle stream) ───
# Deepgram regional endpoint — see the note on _DEEPGRAM_HOST in app.py
# (EU would be ~0.3 s faster per turn but breaks flux-general-multi).
DEEPGRAM_HOST = os.environ.get("DEEPGRAM_API_HOST", "api.deepgram.com")
STT_MODEL = "nova-3"
STT_LANGUAGE = "de"
ENDPOINTING_MS = 1000  # patient semantic VAD (same as the Pi client used)
# STT engine switch: "nova" (default, /v1/listen, silence-based endpointing —
# the fixed ENDPOINTING_MS wait is deliberate: thinking pauses mid-sentence
# must not cut the turn) or "flux" (/v2/listen, model-based turn detection —
# no fixed silence tax; patience knob is ROBBIE_FLUX_EOT_THRESHOLD instead).
# Both engines return the same (transcript, audio, err, word_spans) tuple.
STT_ENGINE = os.environ.get("ROBBIE_STT_ENGINE", "nova").strip().lower()
FLUX_MODEL = "flux-general-multi"  # multilingual Flux; German via language_hint
FLUX_LANGUAGE_HINT = os.environ.get("ROBBIE_FLUX_LANGUAGE_HINT", "de")
# End-of-turn confidence 0..1 (Deepgram default 0.7): raise for more patience
# with thinking pauses, lower for snappier turn ends.
FLUX_EOT_THRESHOLD = float(os.environ.get("ROBBIE_FLUX_EOT_THRESHOLD", "0.7"))
# Words Deepgram should recognise reliably (names, brands, house-specific
# vocabulary). Comma-separated via ROBBIE_STT_KEYTERMS, e.g.
# "Robbie,Anna,Fips,Musterhausen".
KEYTERMS = [
    k.strip()
    for k in os.environ.get("ROBBIE_STT_KEYTERMS", "Robbie,Claude,Anthropic").split(",")
    if k.strip()
]
TURN_NO_SPEECH_SECONDS = 6.0  # give up if nothing is said after the wake
# Hard cap on command length. With Flux, model-based turn detection rides
# out thinking pauses, so long monologues are no longer chunked by silence —
# at 20 s the cap cut a real turn mid-sentence. Under Nova the silence
# endpointing ends turns long before the cap.
TURN_MAX_SECONDS = float(os.environ.get("ROBBIE_TURN_MAX_SECONDS", "40"))
AUDIO_KEEP_MAX_SECONDS = 20.0  # cap the command audio kept per turn

# ── Conversation flow ───────────────────────────────────────────────────────
FOLLOWUP_WINDOW_SECONDS = 6.0  # reply without "Hey Robbie" after Robbie spoke
# Context seam: when the SDK context fills to this %, Robbie summarizes its own
# session into an episode and recycles the session (see entities.SDKWrapper.
# summarize_and_reconnect). Env-overridable so the path can be forced low for
# verification (real 95% is expensive to reach).
CONTEXT_RECONNECT_PCT = int(os.environ.get("ROBBIE_CONTEXT_RECONNECT_PCT", "95"))
# Spoken while the ~3-6s summary+reconnect runs (via _speak_text, no SDK needed).
MAINTENANCE_LINE = (
    "Einen kurzen Moment — ich ordne gerade unser Gespräch. Gleich geht's weiter."
)
# "Robbie stopp" — deterministic conversation kill (no SDK turn, no follow-up
# window). Matches only when the utterance is NOTHING BUT a stop phrase
# (optionally addressed), so "stopp den Timer" or a sentence that merely
# contains "stop" never triggers it. Main use: shutting down the questions a
# false wake provokes, and "Hey Robbie, stopp" as a barge-in that must not
# start a new answer.
STOP_RE = re.compile(
    r"^[\s,.!?]*(?:hey\s+)?(?:robbie|robby|robi|roby)?[\s,.!?]*"
    r"(?:stopp?|halt|schluss|ruhe|sei\s+(?:still|leise))"
    r"[\s,.!?]*$",
    re.IGNORECASE,
)

# The station reports when its playback drained (only for turns that actually
# played audio); if the report never comes (test rig without audio, TTS
# failure), proceed after a cap instead of hanging. The cap is the FLOOR —
# for SDK turns the wait is sized from the audio actually sent (Cartesia
# synthesizes faster than realtime, so a 90 s answer still sits in the
# station's aplay buffer when the runner returns; a fixed 60 s opened the
# follow-up window mid-playback and burned it before the beep).
PLAYBACK_DONE_TIMEOUT = 60.0
PLAYBACK_DONE_MARGIN = 15.0  # WLAN + drain-report slack on top of audio length
TTS_BYTES_PER_SECOND = 48_000  # station audio is 24 kHz mono PCM16

# ── Singleton state (one Pi station) ────────────────────────────────────────
_duplex_ws: WebSocket | None = None

# Station name from the hello message — surfaced by /api/health
_station_name: str | None = None

# openWakeWord model — loaded once, reused across reconnects (~seconds to load)
_oww = None


def _load_wakeword():
    """Load openWakeWord with the hey_robbie classifier (lazy, cached).

    NOTE: never pass ``inference_framework=`` — openwakeword 0.4.0 forwards it
    to AudioFeatures and raises TypeError (same gotcha as on the Pi).
    """
    global _oww
    if _oww is None:
        from openwakeword.model import Model

        logger.info("[duplex] loading wakeword model: %s", WAKE_MODEL_PATH)
        _oww = Model(wakeword_model_paths=[WAKE_MODEL_PATH])
    return _oww


def _reset_wakeword(oww) -> None:
    """Clear openWakeWord's rolling buffers so stale audio can't re-trigger."""
    try:
        oww.reset()
    except Exception:
        try:
            for buf in oww.prediction_buffer.values():
                buf.clear()
        except Exception:
            pass


class RingBuffer:
    """Volatile PCM16 ring buffer: append chunks, read back the last N seconds.

    RAM only — this is the pre-roll source for STT (sentence starts that fall
    before wake detection) and for the wake-segment embedding.
    """

    def __init__(self, seconds: float = RING_SECONDS) -> None:
        self._max_bytes = int(seconds * BYTES_PER_SECOND)
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self.total = 0  # absolute bytes ever appended (offset marks)
        self._dropped = 0  # absolute bytes rotated out

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        self.total += len(chunk)
        while self._size > self._max_bytes and self._chunks:
            dropped = self._chunks.popleft()
            self._size -= len(dropped)
            self._dropped += len(dropped)

    def since(self, offset: int) -> bytes:
        """Return all audio appended after absolute byte ``offset`` (clamped
        to what the ring still holds) — the barge-in pre-roll seed."""
        skip = max(0, offset - self._dropped)
        data = b"".join(self._chunks)
        return data[skip:]

    def tail(self, seconds: float) -> bytes:
        """Return the most recent ``seconds`` of audio (less if not filled)."""
        want = int(seconds * BYTES_PER_SECOND)
        if want <= 0:
            return b""
        parts: list[bytes] = []
        got = 0
        for chunk in reversed(self._chunks):
            parts.append(chunk)
            got += len(chunk)
            if got >= want:
                break
        data = b"".join(reversed(parts))
        return data[-want:] if len(data) > want else data

    @property
    def size_bytes(self) -> int:
        return self._size


class DuplexSession:
    """State for one connected Pi station.

    State machine: listening → (wake) turn → speaking → followup →
    speaking … → listening. A wakeword during "speaking" is a barge-in
    (state "barging" bridges the restart). Everything is driven by feed() —
    one call per binary mic chunk.
    """

    def __init__(self, ws: WebSocket, app_mod) -> None:
        self.ws = ws
        # The LIVE server.app module (passed in by the endpoint — a fresh
        # import here would be a second instance without lifespan globals).
        self.app = app_mod
        self.ring = RingBuffer()
        self.oww = _load_wakeword()
        self.state = "listening"
        self.wake_threshold = WAKE_SCORE_THRESHOLD  # hello may override (tests)
        # Command audio path: while in-turn, feed() mirrors mic chunks into
        # this queue — that IS the pre-roll (queueing starts at wake
        # detection, so nothing said right after the wake is lost).
        self._turn_queue: asyncio.Queue[bytes] | None = None
        self._turn_task: asyncio.Task | None = None
        self.turn_cancel = asyncio.Event()
        self.playback_done = asyncio.Event()  # set by the station's report
        self._stitch = b""  # partial openWakeWord frame between WS chunks
        self._refractory_frames = 0
        # Near-miss instrumentation (same idea as the Pi's app.py): keep the
        # peak of each elevated-but-sub-threshold burst so a missed wake
        # leaves a trace in the logs for calibration.
        self._nm_peak = 0.0
        self._nm_active = False
        self.frames_seen = 0
        self.wakes_seen = 0

    async def feed(self, chunk: bytes) -> None:
        """Consume one binary mic chunk: ring buffer + wakeword detection."""
        self.ring.append(chunk)

        if self._turn_queue is not None:
            self._turn_queue.put_nowait(chunk)

        self._stitch += chunk
        while len(self._stitch) >= OWW_FRAME_BYTES:
            frame = self._stitch[:OWW_FRAME_BYTES]
            self._stitch = self._stitch[OWW_FRAME_BYTES:]
            await self._feed_frame(frame)

    async def _feed_frame(self, frame: bytes) -> None:
        import numpy as np

        self.frames_seen += 1
        samples = np.frombuffer(frame, dtype=np.int16)
        score = float(self.oww.predict(samples)[WAKE_KEY])
        if self._refractory_frames > 0:
            self._refractory_frames -= 1
            return

        # Near-miss burst tracking
        if score >= NEAR_MISS_FLOOR:
            self._nm_active = True
            self._nm_peak = max(self._nm_peak, score)
        elif self._nm_active:
            if self._nm_peak < WAKE_SCORE_THRESHOLD:
                logger.info(
                    "[duplex] near-miss: peak score=%.3f (threshold %.2f)",
                    self._nm_peak,
                    WAKE_SCORE_THRESHOLD,
                )
            self._nm_active = False
            self._nm_peak = 0.0

        if score > self.wake_threshold:
            if self.state == "listening":
                await self._on_wake(score)
            elif self.state == "speaking":
                # Barge-in: Robbie is talking (or the station still plays the
                # tail) and someone says "Hey Robbie" over it — the Jabra's
                # AEC keeps the mic stream clean enough for this. Higher bar
                # than a normal wake (see BARGE_WAKE_THRESHOLD).
                if score >= BARGE_WAKE_THRESHOLD:
                    await self._on_barge(score)
                else:
                    logger.info(
                        "[duplex] barge suppressed: score=%.3f < %.2f",
                        score,
                        BARGE_WAKE_THRESHOLD,
                    )

    async def _on_wake(self, score: float) -> None:
        """Wakeword fired → start a command turn.

        No speaker gate in this edition: every wake is accepted. (A voice-ID
        gate would sit right here, deciding on the last ~1.6 s of the ring
        buffer before the turn starts.)
        """
        self.wakes_seen += 1
        self._refractory_frames = REFRACTORY_FRAMES
        _reset_wakeword(self.oww)
        self._nm_active = False
        self._nm_peak = 0.0

        logger.info(
            "[duplex] wakeword detected: score=%.3f (ring %.1fs)",
            score,
            self.ring.size_bytes / BYTES_PER_SECOND,
        )

        # Command audio starts flowing NOW (pre-roll).
        self._turn_queue = asyncio.Queue()
        try:
            await self.ws.send_json(
                {"type": "wake_accepted", "score": round(score, 3), "speaker": ""}
            )
        except Exception:
            pass

        # TTS prewarm (fire and forget): the answer will need Cartesia in a
        # few seconds — reconnect any dead pool session NOW, hidden behind
        # the user's speaking time, instead of paying the ~1s reconnect on
        # sentence 1 after Cartesia's server-side 5-min idle cut (H4).
        asyncio.create_task(self._prewarm_tts())

        self.state = "turn"
        self._turn_task = asyncio.create_task(self._run_command_turn())

    async def _prewarm_tts(self) -> None:
        """Reconnect dead Cartesia pool sessions off the hot path (wake)."""
        try:
            _, entity_cfg = self.app.entity_manager.get(None)
            tm = getattr(self.app, "_tts_manager", None)
            pool = tm.get_cartesia_pool(entity_cfg.name) if tm else None
            if not pool:
                return
            for session in pool:
                if not session.connected:
                    t0 = asyncio.get_event_loop().time()
                    await session.connect()
                    logger.info(
                        "[duplex] tts prewarm: reconnected pool session (%dms)",
                        int((asyncio.get_event_loop().time() - t0) * 1000),
                    )
        except Exception:
            logger.debug("[duplex] tts prewarm failed", exc_info=True)

    # ── Barge-in (wakeword over Robbie's own voice) ─────────────────────

    async def _on_barge(self, score: float) -> None:
        """"Hey Robbie" while Robbie is talking: kill the station's playback,
        cancel + interrupt the running turn, then restart a fresh command turn
        seeded with everything said since the barge.
        """
        self.wakes_seen += 1
        self._refractory_frames = REFRACTORY_FRAMES
        _reset_wakeword(self.oww)
        self._nm_active = False
        self._nm_peak = 0.0

        # Mark first: the command seed starts here.
        barge_offset = self.ring.total
        logger.info("[duplex] barge-in wakeword: score=%.3f — interrupting", score)

        self.state = "barging"  # blocks wake dispatch until the restart
        try:
            await self.ws.send_json({"type": "abort_playback"})
        except Exception:
            pass
        self.turn_cancel.set()
        self.playback_done.set()  # playback is dead; nobody will report it
        try:
            sdk, _ = self.app.entity_manager.get(None)
            await sdk.interrupt()
        except Exception as exc:
            logger.warning("[duplex] sdk.interrupt failed: %s", exc)

        asyncio.create_task(self._barge_restart(self._turn_task, barge_offset))

    async def _barge_restart(
        self, old_task: asyncio.Task | None, barge_offset: int
    ) -> None:
        """Wait for the interrupted turn to unwind, then start the new one.

        The new turn's queue is seeded from the ring buffer at the barge
        offset — nothing said while the old turn was still unwinding is lost.
        """
        if old_task is not None and not old_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(old_task), timeout=10)
            except Exception:
                old_task.cancel()

        queue: asyncio.Queue[bytes] = asyncio.Queue()
        seed = self.ring.since(barge_offset)
        if seed:
            queue.put_nowait(seed)
        self._turn_queue = queue
        self.state = "turn"
        try:
            await self.ws.send_json(
                {"type": "wake_accepted", "speaker": "", "barge_in": True}
            )
        except Exception:
            pass
        self._turn_task = asyncio.create_task(self._run_command_turn())

    # ── Turn path (STT from the queue → identify → SDK turn → TTS) ──────

    async def _stt_from_queue(
        self, no_speech_timeout: float = TURN_NO_SPEECH_SECONDS
    ) -> tuple[str, bytes, str | None, list[tuple[float, float]]]:
        """Engine dispatcher — actual protocol handling in _stt_nova /
        _stt_flux (same return tuple). Logs one latency line per turn with
        speech: ``tail`` is the trailing silence inside the captured audio
        between the last spoken word and the finalized turn — i.e. the
        endpointing wait the user actually sits through.
        """
        t0 = asyncio.get_event_loop().time()
        if STT_ENGINE == "flux":
            result = await self._stt_flux(no_speech_timeout)
        else:
            result = await self._stt_nova(no_speech_timeout)
        transcript, audio, err, word_spans = result
        if transcript:
            audio_secs = len(audio) / BYTES_PER_SECOND
            tail = audio_secs - word_spans[-1][1] if word_spans else -1.0
            logger.info(
                "[duplex] stt: engine=%s dur=%.1fs tail=%.2fs words=%d",
                STT_ENGINE,
                asyncio.get_event_loop().time() - t0,
                tail,
                len(word_spans),
            )
        return result

    async def _stt_nova(
        self, no_speech_timeout: float = TURN_NO_SPEECH_SECONDS
    ) -> tuple[str, bytes, str | None, list[tuple[float, float]]]:
        """Nova-3 (/v1/listen): stream the queued command audio to Deepgram,
        return (transcript, command_audio, error, word_spans). Ends on
        speech_final (silence endpointing), UtteranceEnd, no-speech timeout
        or the hard cap — the same rules streaming_turn.py used on the Pi.
        ``no_speech_timeout`` is the follow-up window when called from the
        conversation loop.

        ``word_spans`` are the (start, end) second-offsets of the spoken
        words within ``command_audio`` (from the final Deepgram results).
        """
        import aiohttp
        import json as _json
        from urllib.parse import quote

        app_mod = self.app
        if not app_mod._deepgram_api_key or app_mod._proxy_session is None:
            return "", b"", "DEEPGRAM_API_KEY not set"

        dg_url = (
            f"wss://{DEEPGRAM_HOST}/v1/listen"
            f"?model={STT_MODEL}&language={STT_LANGUAGE}"
            f"&encoding=linear16&sample_rate={SAMPLE_RATE}&channels=1"
            f"&punctuate=true&interim_results=true"
            f"&utterance_end_ms=1000&vad_events=true"
            f"&endpointing={ENDPOINTING_MS}"
        )
        for kw in KEYTERMS:
            dg_url += f"&keyterm={quote(kw)}"

        final_segments: list[str] = []
        interim = ""
        err: str | None = None
        audio = bytearray()
        word_spans: list[tuple[float, float]] = []
        got_speech = asyncio.Event()
        done = asyncio.Event()

        async with app_mod._proxy_session.ws_connect(
            dg_url,
            headers={"Authorization": f"Token {app_mod._deepgram_api_key}"},
        ) as dg_ws:

            async def sender() -> None:
                keep_cap = int(AUDIO_KEEP_MAX_SECONDS * BYTES_PER_SECOND)
                while not done.is_set():
                    queue = self._turn_queue
                    if queue is None:
                        break
                    try:
                        chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                    except asyncio.TimeoutError:
                        continue
                    if len(audio) < keep_cap:
                        audio.extend(chunk)
                    try:
                        await dg_ws.send_bytes(chunk)
                    except Exception:
                        break

            async def receiver() -> None:
                nonlocal interim, err
                async for msg in dg_ws:
                    if msg.type != aiohttp.WSMsgType.TEXT:
                        break
                    data = _json.loads(msg.data)
                    t = data.get("type")
                    if t == "Results":
                        alts = (data.get("channel") or {}).get("alternatives") or [{}]
                        text = (alts[0].get("transcript") or "").strip()
                        if text:
                            got_speech.set()
                            if data.get("is_final"):
                                final_segments.append(text)
                                interim = ""
                                for w in alts[0].get("words") or []:
                                    try:
                                        word_spans.append(
                                            (float(w["start"]), float(w["end"]))
                                        )
                                    except (KeyError, TypeError, ValueError):
                                        pass
                            else:
                                interim = text
                        if data.get("speech_final") and (final_segments or interim):
                            if interim:
                                final_segments.append(interim)
                                interim = ""
                            done.set()
                            return
                    elif t == "UtteranceEnd":
                        if interim:
                            final_segments.append(interim)
                            interim = ""
                        if final_segments:
                            done.set()
                            return
                    elif t == "SpeechStarted":
                        got_speech.set()
                    elif t in ("error", "Error"):
                        err = data.get("message") or "unknown STT error"
                        done.set()
                        return

            send_task = asyncio.create_task(sender())
            recv_task = asyncio.create_task(receiver())

            try:
                await asyncio.wait_for(got_speech.wait(), timeout=no_speech_timeout)
            except asyncio.TimeoutError:
                done.set()
            else:
                try:
                    await asyncio.wait_for(done.wait(), timeout=TURN_MAX_SECONDS)
                except asyncio.TimeoutError:
                    done.set()

            try:
                await dg_ws.send_str('{"type": "CloseStream"}')
            except Exception:
                pass
            try:
                await asyncio.wait_for(done.wait(), timeout=0.8)
            except asyncio.TimeoutError:
                pass
            send_task.cancel()
            recv_task.cancel()

        transcript = " ".join(final_segments + ([interim] if interim else [])).strip()
        return transcript, bytes(audio), err, word_spans

    async def _stt_flux(
        self, no_speech_timeout: float = TURN_NO_SPEECH_SECONDS
    ) -> tuple[str, bytes, str | None, list[tuple[float, float]]]:
        """Flux (/v2/listen): model-based turn detection — the model decides
        whether an utterance SOUNDS finished instead of waiting a fixed
        silence (sub-400ms turn ends on clearly-finished commands, patience
        on mid-thought pauses).

        TurnInfo event mapping: StartOfTurn/Update → speech is flowing (the
        freshest transcript+words are kept for the hard-cap path),
        EndOfTurn → turn finalized with word timings. EagerEndOfTurn /
        TurnResumed are ignored on purpose: no speculative SDK turns (every
        aborted attempt would bloat the eternal session context).

        Cartesia Ink 2 uses a near-identical turn-event model (they document
        the Flux mapping themselves) — this method doubles as the template
        for a later Ink switch once it speaks German.
        """
        import aiohttp
        import json as _json
        from urllib.parse import quote

        app_mod = self.app
        if not app_mod._deepgram_api_key or app_mod._proxy_session is None:
            return "", b"", "DEEPGRAM_API_KEY not set", []

        dg_url = (
            f"wss://{DEEPGRAM_HOST}/v2/listen"
            f"?model={FLUX_MODEL}&language_hint={FLUX_LANGUAGE_HINT}"
            f"&encoding=linear16&sample_rate={SAMPLE_RATE}"
            f"&eot_threshold={FLUX_EOT_THRESHOLD}"
        )
        for kw in KEYTERMS:
            dg_url += f"&keyterm={quote(kw)}"

        transcript = ""
        word_spans: list[tuple[float, float]] = []
        err: str | None = None
        audio = bytearray()
        got_speech = asyncio.Event()
        done = asyncio.Event()

        def _take_words(data: dict) -> list[tuple[float, float]]:
            spans: list[tuple[float, float]] = []
            for w in data.get("words") or []:
                try:
                    spans.append((float(w["start"]), float(w["end"])))
                except (KeyError, TypeError, ValueError):
                    pass
            return spans

        try:
            async with app_mod._proxy_session.ws_connect(
                dg_url,
                headers={"Authorization": f"Token {app_mod._deepgram_api_key}"},
            ) as dg_ws:

                async def sender() -> None:
                    keep_cap = int(AUDIO_KEEP_MAX_SECONDS * BYTES_PER_SECOND)
                    while not done.is_set():
                        queue = self._turn_queue
                        if queue is None:
                            break
                        try:
                            chunk = await asyncio.wait_for(queue.get(), timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if len(audio) < keep_cap:
                            audio.extend(chunk)
                        try:
                            await dg_ws.send_bytes(chunk)
                        except Exception:
                            break

                async def receiver() -> None:
                    nonlocal transcript, word_spans, err
                    async for msg in dg_ws:
                        if msg.type != aiohttp.WSMsgType.TEXT:
                            break
                        data = _json.loads(msg.data)
                        t = data.get("type")
                        if t == "TurnInfo":
                            event = data.get("event")
                            text = (data.get("transcript") or "").strip()
                            if text:
                                got_speech.set()
                                # keep the freshest state — EndOfTurn carries
                                # the full turn, Updates cover the hard cap
                                transcript = text
                                word_spans = _take_words(data)
                            if event == "EndOfTurn" and text:
                                done.set()
                                return
                        elif t in ("Error", "error"):
                            err = (
                                data.get("description")
                                or data.get("message")
                                or "unknown STT error"
                            )
                            done.set()
                            return

                send_task = asyncio.create_task(sender())
                recv_task = asyncio.create_task(receiver())

                try:
                    await asyncio.wait_for(
                        got_speech.wait(), timeout=no_speech_timeout
                    )
                except asyncio.TimeoutError:
                    done.set()
                else:
                    try:
                        await asyncio.wait_for(done.wait(), timeout=TURN_MAX_SECONDS)
                    except asyncio.TimeoutError:
                        done.set()  # hard cap — freshest Update state wins

                try:
                    await dg_ws.close()
                except Exception:
                    pass
                send_task.cancel()
                recv_task.cancel()
        except Exception as exc:
            # experimental path: a rejected upgrade (e.g. unsupported query
            # param) must abort the turn softly, not crash the session
            logger.error("[duplex] flux connect/stream failed: %s", exc)
            return "", bytes(audio), f"flux failed: {exc}", []

        return transcript.strip(), bytes(audio), err, word_spans

    # ── Direct speech (capability results + proactive notifications) ────

    async def _speak_text(self, text: str, turn_id: str) -> None:
        """Synthesize one text over the duplex WS using the entity's TTS pool
        — same sentence/audio/turn_end shapes as a normal turn, no SDK.
        """
        app_mod = self.app
        sdk, entity_cfg = app_mod.entity_manager.get(None)
        pool = (
            app_mod._tts_manager.get_cartesia_pool(entity_cfg.name)
            if app_mod._tts_manager
            else None
        )
        if not pool:
            raise RuntimeError("Kein TTS-Pool verbunden")

        await self.ws.send_json(
            {
                "type": "sentence",
                "turn_id": turn_id,
                "text": text,
                "voice_mode": "realtime",
                "seq": 0,
            }
        )
        await self.ws.send_json(
            {
                "type": "audio",
                "turn_id": turn_id,
                "seq": 0,
                "sample_rate": 24000,
                "channels": 1,
                "bits_per_sample": 16,
                "streaming": True,
            }
        )
        try:
            async for chunk in pool[0].synthesize(text):
                await self.ws.send_bytes(chunk)
        except Exception as exc:
            logger.error("[duplex] speak TTS failed: %s", exc)
        await self.ws.send_json({"type": "audio_end", "turn_id": turn_id, "seq": 0})
        await self.ws.send_json(
            {
                "type": "turn_end",
                "turn_id": turn_id,
                "interrupted": False,
                "end_session": False,
            }
        )

    async def notify_consumer(self) -> None:
        """Deliver proactive notifications (timer/api) to the station.

        Waits until the session is idle — a firing timer must not talk over a
        running conversation; it speaks right after. Queue survives while no
        station is connected (delivered on reconnect).
        """
        cm = getattr(self.app, "capability_manager", None)
        if cm is None:
            logger.info("[duplex] no capability manager — notify consumer off")
            return
        queue = cm.notification_queue
        logger.info("[duplex] notify consumer started")
        while True:
            notification = await queue.get()
            if _duplex_ws is not self.ws:
                # Stale consumer (session superseded/tearing down) — hand the
                # notification back so the live session delivers it.
                queue.put_nowait(notification)
                logger.info("[duplex] notify consumer stale — requeued + exit")
                return
            while self.state != "listening":
                await asyncio.sleep(0.5)
            turn_id = f"notify-{id(notification) & 0xFFFFFF:06x}"
            logger.info(
                "[duplex] notify (%s): %s", notification.source, notification.text
            )

            self.state = "speaking"
            self.playback_done.clear()
            try:
                await self.ws.send_json(
                    {"type": "notify", "source": notification.source}
                )
                await self._speak_text(notification.text, turn_id)
                try:
                    await asyncio.wait_for(
                        self.playback_done.wait(), timeout=PLAYBACK_DONE_TIMEOUT
                    )
                except asyncio.TimeoutError:
                    pass
            except Exception as exc:
                # WS is likely dead — requeue so the notification survives the
                # reconnect; the handler's finally cleans this task up.
                queue.put_nowait(notification)
                logger.error("[duplex] notify delivery failed (requeued): %s", exc)
                return
            finally:
                if self.state == "speaking":
                    self.state = "listening"

    async def _run_command_turn(self) -> None:
        """Gate accepted → STT → identify → run the turn through the same
        streaming runner the classic /voice/stream path uses (the station
        receives the identical sentence/audio/turn_end messages).

        After each answer a follow-up window opens (mic stays hot, no
        wakeword needed) — every reply extends the conversation, a silent
        window ends it. The speaker label is sticky across follow-ups.
        """
        app_mod = self.app
        turn_id = None
        try:
            t0 = asyncio.get_event_loop().time()
            transcript, cmd_audio, err, _ = await self._stt_from_queue()
            self._turn_queue = None
            t_stt_done = asyncio.get_event_loop().time()

            if err or not transcript:
                reason = err or "no_speech"
                logger.info("[duplex] turn aborted: %s", reason)
                try:
                    await self.ws.send_json(
                        {"type": "turn_aborted", "reason": reason}
                    )
                except Exception:
                    pass
                return

            # No speaker identification in this edition — the label stays
            # empty and the LLM addresses whoever is there neutrally.
            speaker = ""
            logger.info(
                "[duplex] transcript (%.1fs after wake): text=%s",
                asyncio.get_event_loop().time() - t0,
                transcript[:80],
            )

            import uuid as _uuid

            while True:  # conversation loop: first turn + follow-ups
                turn_id = _uuid.uuid4().hex[:12]

                try:
                    await self.ws.send_json(
                        {
                            "type": "turn_transcript",
                            "turn_id": turn_id,
                            "text": transcript,
                            "speaker": speaker,
                        }
                    )
                except Exception:
                    pass

                # "Robbie stopp": end the conversation right here — no answer,
                # no follow-up window. The station gets a turn_aborted with
                # reason "stop" (soft cue, not an error tone).
                if STOP_RE.match(transcript):
                    logger.info("[duplex] stop command — conversation ended")
                    try:
                        await self.ws.send_json(
                            {
                                "type": "turn_aborted",
                                "reason": "stop",
                                "turn_id": turn_id,
                            }
                        )
                    except Exception:
                        pass
                    return

                # Deterministic capability commands (timer) bypass the
                # LLM entirely — regex match, spoken confirmation, done.
                cm = getattr(app_mod, "capability_manager", None)
                matched = cm.match_command(transcript) if cm else None
                used_sdk = False  # only SDK turns grow the context window
                audio_bytes_sent = 0  # sizes the drain wait below
                if matched is not None:
                    cap_name, cmd = matched
                    logger.info(
                        "[duplex] capability %s: %s", cap_name, cmd.text[:80]
                    )
                    self.state = "speaking"
                    self.turn_cancel = asyncio.Event()
                    self.playback_done.clear()
                    await self._speak_text(cmd.text, turn_id)
                else:
                    used_sdk = True
                    try:
                        sdk, entity_cfg = app_mod.entity_manager.get(None)
                    except Exception as exc:
                        await self.ws.send_json(
                            {"type": "error", "message": str(exc)}
                        )
                        return
                    if not sdk.connected or sdk.turn_active:
                        await self.ws.send_json(
                            {
                                "type": "error",
                                "message": "Entity belegt oder nicht verbunden",
                            }
                        )
                        return
                    pool = (
                        app_mod._tts_manager.get_cartesia_pool(entity_cfg.name)
                        if app_mod._tts_manager
                        else None
                    )
                    if not pool:
                        await self.ws.send_json(
                            {"type": "error", "message": "Kein TTS-Pool verbunden"}
                        )
                        return

                    self.state = "speaking"
                    empty_answer = False
                    for attempt in (1, 2):
                        self.turn_cancel = asyncio.Event()
                        self.playback_done.clear()
                        result = await app_mod._voice_turn_runner_streaming(
                            self.ws,
                            sdk,
                            entity_cfg,
                            turn_id,
                            transcript,
                            self.turn_cancel,
                            pool,
                            speaker=speaker,
                            t_stt_done=t_stt_done,
                        )
                        n_sentences = result[0] if isinstance(result, tuple) else None
                        if isinstance(result, tuple) and len(result) > 2:
                            audio_bytes_sent = result[2]

                        if self.turn_cancel.is_set():
                            # Barge-in (restart runs elsewhere) or teardown.
                            return
                        if n_sentences == 0:
                            # The SDK sometimes returns an empty answer for
                            # the first query right after interrupt()
                            # (observed after barge-in). One silent retry.
                            if attempt == 1:
                                logger.warning(
                                    "[duplex] empty answer (0 sentences) — retrying once"
                                )
                                continue
                            empty_answer = True
                        break

                    if empty_answer:
                        logger.warning("[duplex] empty answer twice — giving up")
                        try:
                            await self.ws.send_json(
                                {"type": "turn_aborted", "reason": "empty_answer"}
                            )
                        except Exception:
                            pass
                        return

                # Wait until the station's aplay actually drained — opening
                # the follow-up mic earlier would transcribe Robbie's tail.
                # Sized from the audio actually sent: long answers outlive
                # any fixed cap in the station's playback buffer.
                drain_timeout = max(
                    PLAYBACK_DONE_TIMEOUT,
                    audio_bytes_sent / TTS_BYTES_PER_SECOND + PLAYBACK_DONE_MARGIN,
                )
                try:
                    await asyncio.wait_for(
                        self.playback_done.wait(), timeout=drain_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[duplex] no playback_done report after %.0fs "
                        "(%.1fs audio sent) — proceeding anyway",
                        drain_timeout,
                        audio_bytes_sent / TTS_BYTES_PER_SECOND,
                    )

                # Context seam (95%): off the hot path — Robbie already spoke.
                # Only SDK turns grow the context, so skip the (50-200ms) usage
                # probe after capability replies.
                if used_sdk:
                    try:
                        pct = await sdk.context_pct()
                    except Exception:
                        pct = 0
                    if pct >= CONTEXT_RECONNECT_PCT:
                        logger.info(
                            "[duplex] context at %d%% — summarize + reconnect", pct
                        )
                        self.state = "maintenance"
                        maint_id = _uuid.uuid4().hex[:12]
                        self.playback_done.clear()
                        try:
                            await self._speak_text(MAINTENANCE_LINE, maint_id)
                            await asyncio.wait_for(
                                self.playback_done.wait(),
                                timeout=PLAYBACK_DONE_TIMEOUT,
                            )
                        except Exception:
                            pass
                        await sdk.summarize_and_reconnect()
                        # End here; the next "Hey Robbie" starts fresh with the
                        # episode injected into its first turn.
                        return

                if matched is not None and matched[1].end_conversation:
                    logger.info(
                        "[duplex] capability closed the conversation — no follow-up"
                    )
                    return

                if FOLLOWUP_WINDOW_SECONDS <= 0:
                    return

                # Follow-up window: mic hot, no wakeword needed.
                self.state = "followup"
                self._turn_queue = asyncio.Queue()
                try:
                    await self.ws.send_json(
                        {"type": "listening", "window": FOLLOWUP_WINDOW_SECONDS}
                    )
                except Exception:
                    pass
                transcript, cmd_audio, err, _ = await self._stt_from_queue(
                    no_speech_timeout=FOLLOWUP_WINDOW_SECONDS
                )
                self._turn_queue = None
                t_stt_done = asyncio.get_event_loop().time()
                if err or not transcript:
                    logger.info(
                        "[duplex] follow-up window closed (%s)", err or "silence"
                    )
                    return
                logger.info("[duplex] follow-up transcript: text=%s", transcript[:80])
        except Exception as exc:
            logger.error("[duplex] command turn failed: %s", exc, exc_info=True)
            try:
                await self.ws.send_json(
                    {"type": "error", "message": f"Turn-Fehler: {exc}", "turn_id": turn_id}
                )
            except Exception:
                pass
        finally:
            self._turn_queue = None
            self.state = "listening"


async def handle_duplex(ws: WebSocket, app_mod) -> None:
    """WS handler for /voice/duplex — accept, enforce singleton, pump frames.

    ``app_mod`` is the live server.app module (lifespan globals: deepgram key,
    proxy session, entity/TTS managers, turn runner).
    """
    global _duplex_ws, _station_name

    if _duplex_ws is not None:
        await ws.accept()
        await ws.send_json(
            {"type": "error", "message": "Bereits eine Duplex-Station verbunden"}
        )
        await ws.close(code=1008, reason="Already connected")
        return

    # Fail loudly (before accept-and-hang) if the wakeword stack is missing.
    try:
        _load_wakeword()
    except Exception as exc:
        logger.error("[duplex] voice stack init failed: %s", exc)
        await ws.accept()
        await ws.send_json(
            {"type": "error", "message": f"Voice-Init fehlgeschlagen: {exc}"}
        )
        await ws.close(code=1011)
        return

    await ws.accept()
    _duplex_ws = ws
    session = DuplexSession(ws, app_mod)
    notify_task = asyncio.create_task(session.notify_consumer())
    logger.info("[duplex] station connected (%s)", ws.client)

    try:
        while True:
            data = await ws.receive()
            if data["type"] == "websocket.disconnect":
                break
            if data.get("bytes"):
                await session.feed(data["bytes"])
            elif data.get("text"):
                await _handle_control(ws, session, data["text"])
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        logger.error("[duplex] error: %s", exc, exc_info=True)
    finally:
        logger.info("[duplex] station teardown begins")
        _duplex_ws = None
        _station_name = None
        notify_task.cancel()
        try:
            await notify_task
        except (asyncio.CancelledError, Exception):
            pass
        session.turn_cancel.set()
        if session._turn_task is not None and not session._turn_task.done():
            session._turn_task.cancel()
            try:
                await session._turn_task
            except (asyncio.CancelledError, Exception):
                pass
        _reset_wakeword(session.oww)
        logger.info(
            "[duplex] station disconnected (frames=%d, wakes=%d)",
            session.frames_seen,
            session.wakes_seen,
        )


async def _handle_control(ws: WebSocket, session: DuplexSession, raw: str) -> None:
    import json

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        await ws.send_json({"type": "error", "message": "Ungültiges JSON"})
        return

    global _station_name

    msg_type = data.get("type", "")
    if msg_type == "hello":
        _station_name = str(data.get("station") or "") or None
        # Optional wakeword-threshold override — test/calibration knob only
        # (lets a replay client push a clip through with a lower bar).
        if data.get("wake_threshold") is not None:
            session.wake_threshold = float(data["wake_threshold"])
            logger.info(
                "[duplex] wake threshold override: %.3f", session.wake_threshold
            )
        logger.info(
            "[duplex] hello from station=%s entity=%s",
            data.get("station", "?"),
            data.get("entity", "robbie"),
        )
        await ws.send_json({"type": "hello_ack", "entity": data.get("entity", "robbie")})
    elif msg_type == "playback_done":
        session.playback_done.set()
    elif msg_type == "ping":
        await ws.send_json({"type": "pong"})
    else:
        logger.debug("[duplex] unknown control type: %s", msg_type)
