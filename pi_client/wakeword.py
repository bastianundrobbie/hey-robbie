#!/usr/bin/env python3
"""Wakeword detection for the Pi speech station (step 4).

openWakeWord runs the bundled melspectrogram + embedding ONNX feature models
plus our custom "hey_robbie" classifier over a continuous arecord 16 kHz stream.
On a score above threshold the wakeword is detected.

The melspec/embedding feature models ship inside the openwakeword package
(resources/models) — only models/hey_robbie.onnx is ours.

Standalone use:
    python wakeword.py --test              # print scores, beep on detect, loop
    python wakeword.py --test --seconds 25 # auto-stop after 25 s (calibration)

As a library: make_detector() + wait_for_wakeword() are reused by app.py.
"""

import argparse
import subprocess
import sys
import time

import numpy as np
from openwakeword.model import Model

import common

FRAME_SAMPLES = 1280               # 80 ms @ 16 kHz — openWakeWord's chunk size
FRAME_BYTES = FRAME_SAMPLES * 2
MODEL_PATH = "models/hey_robbie.onnx"
WAKEWORD_KEY = "hey_robbie"
DEFAULT_THRESHOLD = 0.4  # real "Hey Robbie" now scores ~0.5-0.7 at table distance;
                         # 0.5 missed too many. Noise/partials stay < 0.4 (calibrate
                         # with `wakeword.py --test` if false triggers creep in).


def make_detector(model_path: str = MODEL_PATH) -> Model:
    """Load openWakeWord with our hey_robbie classifier (one-time ~5 s)."""
    return Model(wakeword_model_paths=[model_path])


def open_capture(dev: str) -> subprocess.Popen:
    """Start a continuous raw 16 kHz mono capture from the Jabra."""
    return subprocess.Popen(
        ["arecord", "-D", dev, "-f", "S16_LE", "-r", str(common.CAPTURE_RATE),
         "-c", "1", "-t", "raw", "-q"],
        stdout=subprocess.PIPE,
    )


def wait_for_wakeword(
    oww: Model,
    proc: subprocess.Popen,
    threshold: float = DEFAULT_THRESHOLD,
    *,
    on_score=None,
    deadline: float | None = None,
) -> float | None:
    """Block reading frames until the wakeword score exceeds threshold.

    Returns the triggering score, None if the capture stream ended, or the
    sentinel -1.0 if `deadline` (monotonic time) is reached first.
    """
    assert proc.stdout is not None
    while True:
        if deadline is not None and time.monotonic() > deadline:
            return -1.0
        buf = proc.stdout.read(FRAME_BYTES)
        if not buf or len(buf) < FRAME_BYTES:
            return None
        frame = np.frombuffer(buf, dtype=np.int16)
        score = float(oww.predict(frame)[WAKEWORD_KEY])
        if on_score is not None:
            on_score(score)
        if score > threshold:
            return score


def reset_detector(oww: Model) -> None:
    """Clear openWakeWord's internal audio buffers between turns."""
    try:
        oww.reset()
    except Exception:
        # Older/newer API without reset() — fall back to clearing buffers.
        try:
            for buf in oww.prediction_buffer.values():
                buf.clear()
        except Exception:
            pass


def main() -> None:
    ap = argparse.ArgumentParser(description="Wakeword 'Hey Robbie' detector")
    ap.add_argument("--dev", default=common.ALSA_DEV, help="ALSA device name")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    ap.add_argument("--test", action="store_true",
                    help="print scores and beep on detect, keep looping")
    ap.add_argument("--seconds", type=float, default=0.0,
                    help="auto-stop after N seconds (0 = run forever)")
    args = ap.parse_args()

    print("[oww] loading model...", file=sys.stderr)
    oww = make_detector()
    proc = open_capture(args.dev)
    print(f"[oww] listening for 'Hey Robbie' (threshold {args.threshold})", file=sys.stderr)
    # Audible "ready" cue (low double-blip) so the speaker knows when to talk.
    common.beep(args.dev, freq=440, ms=90)

    deadline = time.monotonic() + args.seconds if args.seconds else None
    peak = 0.0

    def on_score(s: float) -> None:
        nonlocal peak
        peak = max(peak, s)
        if s > 0.1:
            print(f"[oww]   score={s:.3f}", flush=True, file=sys.stderr)

    try:
        while True:
            score = wait_for_wakeword(
                oww, proc, args.threshold,
                on_score=on_score if args.test else None,
                deadline=deadline,
            )
            if score is None:
                print("[oww] capture ended", file=sys.stderr)
                break
            if score < 0:  # deadline reached
                break
            print(f"[oww] *** DETECTED *** score={score:.3f}", flush=True, file=sys.stderr)
            common.beep(args.dev, freq=1200, ms=120)
            reset_detector(oww)
            if not args.test:
                break
        if args.test:
            print(f"[oww] peak score seen: {peak:.3f}", file=sys.stderr)
    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
