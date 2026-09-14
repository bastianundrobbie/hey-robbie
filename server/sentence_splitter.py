"""Sentence splitter and text-delta extraction for streaming TTS.

Splits buffered text at sentence boundaries (`.`, `!`, `?` + whitespace),
respecting abbreviations (Dr., z.B., etc.) and minimum sentence length.

Also provides ``extract_text_delta`` to pull text chunks out of Claude
SDK ``StreamEvent`` objects.

Shared between ``core.claude_client`` (Voice-Client) and
``server.app`` (API-Server) to avoid code duplication.
"""

from __future__ import annotations

import re

# Bekannte Abkürzungen die NICHT als Satzende gelten (lowercase).
_ABBREVIATIONS: frozenset[str] = frozenset(
    {
        "z.b.",
        "d.h.",
        "u.a.",
        "o.ä.",
        "s.o.",
        "s.u.",
        "bzw.",
        "dr.",
        "mr.",
        "mrs.",
        "ms.",
        "prof.",
        "nr.",
        "ca.",
        "inkl.",
        "evtl.",
        "ggf.",
        "usw.",
        "etc.",
        "resp.",
        "tel.",
        "max.",
        "min.",
        "abs.",
        "zzgl.",
        "mio.",
        "mrd.",
        "std.",
        "str.",
    }
)

# Regex: Satzende-Zeichen gefolgt von Whitespace.
# Gruppe 1 = Satz bis inkl. Satzzeichen + optionale Anführungszeichen,
# Gruppe 2 = Separator-Whitespace.
_SENTENCE_END_RE = re.compile(
    r"""
    (                       # Gruppe 1: Satz bis inkl. Satzzeichen
        .*?                 # Beliebiger Text (non-greedy)
        [.!?]               # Satzende-Zeichen
        [»"'"\)]*           # Optionale schließende Anführungszeichen/Klammern
    )
    (                       # Gruppe 2: Separator nach Satzzeichen
        \s+                 # Mindestens ein Whitespace
    )
    """,
    re.DOTALL | re.VERBOSE,
)

# Pre-compiled regex for extracting the last word ending with a period.
_LAST_WORD_RE = re.compile(r"(\S+[.])$")

# Minimale Satzlänge bevor wir splitten.
# Verhindert dass "Ja." oder "OK." sofort als eigener Satz rausfallen
# wenn noch mehr Text nachkommt.
_MIN_SENTENCE_LEN = 20

# Maximum sentence length before forced clause-boundary split.
# Prevents very long sentences that cause multi-second TTS streaming delays.
# 150 chars ≈ 25 words ≈ 5–7 seconds of German speech.
_MAX_SENTENCE_CHARS = 150

# Shorter limit for the first sentence of a turn (Quick-First-Sentence).
# Gets TTS started earlier — the user hears something sooner.
# 80 chars ≈ 13 words ≈ 2–3 seconds of German speech.
_MAX_FIRST_SENTENCE_CHARS = 80

# Natural clause boundaries for forced splitting (ordered by preference).
_CLAUSE_SEPARATORS = [
    "; ",
    ", ",
    " – ",
    " — ",
    ": ",
    " und ",
    " oder ",
    " aber ",
    " denn ",
]


def split_long_sentence(
    sentence: str, *, max_chars: int = _MAX_SENTENCE_CHARS
) -> list[str]:
    """Split a sentence exceeding max length at natural clause boundaries.

    Tries clause separators (semicolon, comma, dash, colon, conjunctions)
    before falling back to word boundaries.  Returns a single-element
    list when the sentence is within limits.

    Parameters
    ----------
    max_chars:
        Maximum character count before forcing a split.  Use
        ``_MAX_FIRST_SENTENCE_CHARS`` for the first sentence of a turn
        to reduce Time-to-First-Audio.
    """
    if len(sentence) <= max_chars:
        return [sentence]

    parts: list[str] = []
    remaining = sentence

    while len(remaining) > max_chars:
        best_pos = -1

        # Find the last clause separator before the limit
        for sep in _CLAUSE_SEPARATORS:
            pos = remaining.rfind(sep, 0, max_chars)
            if pos > best_pos and pos > 20:  # min 20 chars before split
                best_pos = pos + len(sep)

        if best_pos <= 20:
            # No good clause boundary — fall back to last space
            best_pos = remaining.rfind(" ", 0, max_chars)
            if best_pos <= 20:
                break  # Can't split reasonably — keep as is

        part = remaining[:best_pos].rstrip()
        if part:
            parts.append(part)
        remaining = remaining[best_pos:].lstrip()

        # After the first part, revert to the standard limit so only
        # the leading fragment benefits from the shorter threshold.
        max_chars = _MAX_SENTENCE_CHARS

    if remaining.strip():
        parts.append(remaining.strip())

    return parts if parts else [sentence]


def split_sentences(
    buffer: str, min_len: int = _MIN_SENTENCE_LEN
) -> tuple[list[str], str]:
    """Splitte gepufferten Text in fertige Sätze und Rest.

    Returns:
        (sentences, remaining_buffer)

    Abkürzungen wie "z.B." oder "Dr." werden nicht als Satzende
    gewertet. Kurze Fragmente (<20 Zeichen) werden erst gesplittet
    wenn ein weiterer Satz folgt.
    """
    sentences: list[str] = []
    remaining = buffer
    prefix = ""  # Text consumed by abbreviation skips

    while True:
        m = _SENTENCE_END_RE.match(remaining)
        if m is None:
            break

        candidate = m.group(1)
        rest_after = remaining[m.end() :]

        # Prüfe ob das Satzende eigentlich eine Abkürzung ist.
        last_word_match = _LAST_WORD_RE.search(candidate)
        if last_word_match:
            last_token = last_word_match.group(1).lower()
            if last_token in _ABBREVIATIONS:
                # Keep the matched text (incl. separator) as prefix
                # so it is not lost from the next real sentence.
                prefix += remaining[: m.end()]
                remaining = rest_after
                continue

        # Prepend any accumulated abbreviation prefix
        full_candidate = prefix + candidate
        prefix = ""

        # Kurze Sätze nur splitten wenn danach noch Text kommt.
        stripped = full_candidate.strip()
        if len(stripped) < min_len and not rest_after.strip():
            break

        if stripped:
            for sub in split_long_sentence(stripped):
                sentences.append(sub)
        remaining = rest_after

    # If we have an unsplit prefix, prepend it to remaining
    remaining = prefix + remaining
    return sentences, remaining


# =====================================================================
#  Text-Delta Extraktion aus SDK StreamEvents
# =====================================================================


def extract_text_delta(msg: object) -> str:
    """Extract text from an SDK ``StreamEvent`` (text_delta).

    Inspects the nested event structure::

        msg.event = {'type': 'content_block_delta',
                     'delta': {'type': 'text_delta', 'text': '...'}}

    Returns:
        The text chunk, or empty string if *msg* is not a text_delta event.
    """
    event = getattr(msg, "event", None)
    if not isinstance(event, dict):
        return ""

    if event.get("type") != "content_block_delta":
        return ""

    delta = event.get("delta", {})
    if not isinstance(delta, dict):
        return ""

    if delta.get("type") != "text_delta":
        return ""

    return delta.get("text", "")
