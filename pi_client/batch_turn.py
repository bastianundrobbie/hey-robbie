#!/usr/bin/env python3
"""Batch-turn voice client for the Pi 3 + Jabra speech station (bring-up step 2).

One full turn using fixed-duration recording + batch STT:

    beep cue -> arecord (16 kHz mono, fixed duration) -> POST /api/stt -> transcript
             -> WS /voice/stream: entity_wake + turn_start
             -> collect sentence text + PCM16 audio until turn_end
             -> upsample 24 kHz mono -> 48 kHz stereo -> aplay

Shared helpers live in common.py. For live streaming STT with semantic VAD
(no fixed duration), see streaming_turn.py (step 3).

Modes:
    (default)      full mic turn: record fixed duration, transcribe, converse.
    --text "..."   skip mic + STT, drive the WS turn with a fixed transcript.
                   Verifies the WS + TTS + playback path without speaking.
"""

import argparse
import sys

import common


def main() -> None:
    ap = argparse.ArgumentParser(description="Pi batch-turn voice client")
    ap.add_argument("--http", default=common.HTTP_BASE, help="server HTTP base")
    ap.add_argument("--ws", default=common.WS_BASE, help="server WS base")
    ap.add_argument("--dev", default=common.ALSA_DEV, help="ALSA device name")
    ap.add_argument("--seconds", type=float, default=5.0, help="record duration (mic mode)")
    ap.add_argument("--text", default=None,
                    help="skip mic + STT; drive the turn with this transcript")
    ap.add_argument("--speaker", default="",
                    help="speaker label to send with turn_start (voice-ID test)")
    ap.add_argument("--no-beep", action="store_true", help="skip the record cue")
    args = ap.parse_args()

    if args.text is not None:
        transcript = args.text.strip()
        print(f"[text] using fixed transcript: \"{transcript}\"", file=sys.stderr)
    else:
        if not args.no_beep:
            common.beep(args.dev)
        pcm16 = common.record(args.dev, args.seconds)
        transcript = common.transcribe(args.http, pcm16)
        if not transcript:
            sys.exit("[stt] empty transcript — nothing to send")

    sentences, pcm, rate = common.run_turn(args.ws, transcript, speaker=args.speaker)
    if not pcm:
        print("[play] no audio received — check server logs (API key missing or invalid?)",
              file=sys.stderr)
        sys.exit(1)

    common.play_response(args.dev, pcm, rate)
    print("[done]", file=sys.stderr)


if __name__ == "__main__":
    main()
