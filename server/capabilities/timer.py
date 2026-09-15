"""Timer Capability — Countdown-Timer für den Familienassistenten.

Verwaltet Countdown-Timer ("Timer 5 Minuten", "Timer 10 Minuten Nudeln").
Bei Ablauf wird eine proaktive Benachrichtigung über den Voice-WebSocket
gesendet — Robbie sagt "Timer abgelaufen!".

Unterstützte Sprachbefehle (Regex-basiert):
- "Timer X Minuten [Label]"     → Timer setzen
- "Timer X Sekunden [Label]"    → Timer setzen (Sekunden)
- "Timer stopp/abbrechen/aus"   → Alle Timer löschen
- "Welche Timer / Timer Status" → Aktive Timer auflisten
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from server.capabilities import BaseCapability, CommandResult, Notification

logger = logging.getLogger(__name__)


@dataclass
class TimerEntry:
    """A single countdown timer."""

    id: int
    label: str
    duration_seconds: float
    created_at: datetime
    ends_at: datetime
    handle: asyncio.TimerHandle | None = None
    fired: bool = False


class TimerCapability(BaseCapability):
    """Countdown timer capability with voice command matching."""

    def __init__(self, timezone: str = "Europe/Berlin") -> None:
        self._tz = ZoneInfo(timezone)
        self._timers: dict[int, TimerEntry] = {}
        self._next_id = 1
        self._pending_notifications: list[Notification] = []
        self._loop: asyncio.AbstractEventLoop | None = None

    @property
    def name(self) -> str:
        return "timer"

    async def startup(self) -> None:
        self._loop = asyncio.get_running_loop()
        logger.info("[timer] Timer capability ready")

    async def shutdown(self) -> None:
        # Cancel all active timers
        for entry in self._timers.values():
            if entry.handle and not entry.fired:
                entry.handle.cancel()
        self._timers.clear()
        logger.info("[timer] All timers cancelled, shutdown complete")

    def match_command(self, transcript: str) -> CommandResult | None:
        """Match German voice commands for timer operations."""
        text = transcript.lower().strip()

        # ── Cancel all timers ──
        if re.search(
            r"\btimer\b.*\b(stopp|stop|abbrechen|aus|löschen|lösche|cancel|weg)\b", text
        ):
            return self._cancel_all_sync()

        # ── List timers ──
        if re.search(
            r"\b(welche|wie\s*viele?|aktive|laufende)\b.*\btimer\b|\btimer\b.*\b(status|liste|übersicht)\b",
            text,
        ):
            return self._list_timers_sync()

        # ── Set timer: "timer X minuten/sekunden [label]" ──
        # Patterns:
        #   "timer 5 minuten"
        #   "timer 5 minuten nudeln"
        #   "timer auf 5 minuten"
        #   "stell einen timer auf 5 minuten"
        #   "timer nudeln 5 minuten"
        #   "5 minuten timer"
        #   "timer 30 sekunden"
        #   "timer eine halbe stunde"
        #   "timer eineinhalb stunden"
        #   "timer anderthalb stunden"

        # Handle special duration words
        duration_minutes = self._parse_duration(text)
        if duration_minutes is not None and re.search(r"\btimer\b", text):
            label = self._extract_label(text)
            return self._set_timer_sync(duration_minutes, label)

        return None

    # German number words → numeric value
    _GERMAN_NUMBERS: dict[str, float] = {
        "eine": 1,
        "ein": 1,
        "eins": 1,
        "zwei": 2,
        "zwo": 2,
        "drei": 3,
        "vier": 4,
        "fünf": 5,
        "sechs": 6,
        "sieben": 7,
        "acht": 8,
        "neun": 9,
        "zehn": 10,
        "elf": 11,
        "zwölf": 12,
        "dreizehn": 13,
        "vierzehn": 14,
        "fünfzehn": 15,
        "zwanzig": 20,
        "dreißig": 30,
        "dreissig": 30,
        "vierzig": 40,
        "fünfzig": 50,
        "sechzig": 60,
    }

    def _parse_number(self, text: str) -> float | None:
        """Try to parse a German number word or digit from text in front of a unit."""
        # Try digit first
        m = re.search(r"(\d+(?:[.,]\d+)?)\s*(?:minuten?|sekunden?|stunden?)", text)
        if m:
            return float(m.group(1).replace(",", "."))
        # Try German number words
        pattern = (
            r"\b("
            + "|".join(self._GERMAN_NUMBERS.keys())
            + r")\s+(?:minuten?|sekunden?|stunden?)"
        )
        m = re.search(pattern, text)
        if m:
            return self._GERMAN_NUMBERS[m.group(1)]
        return None

    def _parse_duration(self, text: str) -> float | None:
        """Parse German duration from text. Returns minutes or None."""

        # "eineinhalb stunden" / "anderthalb stunden"
        if re.search(r"\b(eineinhalb|anderthalb)\s+stunden?\b", text):
            return 90.0

        # "eine halbe stunde" / "halbe stunde"
        if re.search(r"\b(eine?\s+)?halbe\s+stunde\b", text):
            return 30.0

        # "eine dreiviertel stunde" / "dreiviertel stunde"
        if re.search(r"\b(eine?\s+)?dreiviertel\s+stunde\b", text):
            return 45.0

        # "eine stunde" (word, no digits)
        if re.search(r"\beine\s+stunde\b", text) and not re.search(r"\d", text):
            return 60.0

        # "eine minute" / "eine sekunde" (word, no digits)
        if re.search(r"\beine\s+minute\b", text) and not re.search(r"\d", text):
            return 1.0
        if re.search(r"\beine\s+sekunde\b", text) and not re.search(r"\d", text):
            return 1.0 / 60.0

        # Build number word pattern for compound matching
        _nw = "|".join(self._GERMAN_NUMBERS.keys())
        _num = rf"(?:\d+(?:[.,]\d+)?|{_nw})"

        # "X stunden Y minuten" (digits or words)
        m = re.search(rf"\b({_num})\s*stunden?\s+(?:und\s+)?({_num})\s*minuten?", text)
        if m:
            hours = self._GERMAN_NUMBERS.get(m.group(1), None)
            if hours is None:
                hours = float(m.group(1).replace(",", "."))
            mins = self._GERMAN_NUMBERS.get(m.group(2), None)
            if mins is None:
                mins = float(m.group(2).replace(",", "."))
            return hours * 60.0 + mins

        # "X stunden" (digits or words)
        m = re.search(rf"\b({_num})\s*stunden?", text)
        if m:
            val = self._GERMAN_NUMBERS.get(m.group(1), None)
            if val is None:
                val = float(m.group(1).replace(",", "."))
            return val * 60.0

        # "X minuten" (digits or words)
        m = re.search(rf"\b({_num})\s*minuten?", text)
        if m:
            val = self._GERMAN_NUMBERS.get(m.group(1), None)
            if val is None:
                val = float(m.group(1).replace(",", "."))
            return val

        # "X sekunden" (digits or words)
        m = re.search(rf"\b({_num})\s*sekunden?", text)
        if m:
            val = self._GERMAN_NUMBERS.get(m.group(1), None)
            if val is None:
                val = float(m.group(1).replace(",", "."))
            return val / 60.0

        return None

    def _extract_label(self, text: str) -> str:
        """Extract optional label from timer command."""
        # Remove common timer command words
        _nw = "|".join(self._GERMAN_NUMBERS.keys())
        cleaned = re.sub(
            rf"\b(stell|stelle|einen|eine|ein|den|timer|auf|für|von|"
            rf"\d+(?:[.,]\d+)?\s*(?:minuten?|sekunden?|stunden?)|"
            rf"(?:{_nw})\s*(?:minuten?|sekunden?|stunden?)|"
            rf"eineinhalb|anderthalb|halbe|dreiviertel|"
            rf"stunde|minute|sekunde|und|bitte|mal|mir|noch)\b",
            "",
            text,
            flags=re.IGNORECASE,
        ).strip()
        # Clean up whitespace
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        # Remove leading/trailing punctuation
        cleaned = cleaned.strip(".,!? ")
        # Capitalize first letter if we have something
        if cleaned:
            cleaned = cleaned[0].upper() + cleaned[1:]
        return cleaned

    def _set_timer_sync(self, minutes: float, label: str) -> CommandResult:
        """Set a timer and return confirmation text."""
        if self._loop is None:
            return CommandResult(text="Timer-System noch nicht bereit.")

        if minutes <= 0:
            return CommandResult(text="Die Dauer muss größer als null sein.")

        if minutes > 24 * 60:
            return CommandResult(text="Timer können maximal 24 Stunden lang sein.")

        timer_id = self._next_id
        self._next_id += 1

        now = datetime.now(self._tz)
        ends_at = now + timedelta(minutes=minutes)
        seconds = minutes * 60.0

        entry = TimerEntry(
            id=timer_id,
            label=label,
            duration_seconds=seconds,
            created_at=now,
            ends_at=ends_at,
        )

        # Schedule the callback
        entry.handle = self._loop.call_later(seconds, self._on_timer_fired, timer_id)
        self._timers[timer_id] = entry

        # Build confirmation text
        duration_text = self._format_duration(minutes)
        if label:
            confirm = f"Timer für {label} auf {duration_text} gestellt."
        else:
            confirm = f"Timer auf {duration_text} gestellt."

        logger.info(
            "[timer] Set timer #%d: %s (%s)",
            timer_id,
            duration_text,
            label or "kein Label",
        )
        return CommandResult(text=confirm, behaviors=["nodding"])

    def _cancel_all_sync(self) -> CommandResult:
        """Cancel all timers."""
        count = 0
        for entry in self._timers.values():
            if entry.handle and not entry.fired:
                entry.handle.cancel()
                count += 1
        self._timers.clear()

        if count == 0:
            return CommandResult(text="Es laufen keine Timer.")
        elif count == 1:
            return CommandResult(text="Timer abgebrochen.")
        else:
            return CommandResult(text=f"Alle {count} Timer abgebrochen.")

    def _list_timers_sync(self) -> CommandResult:
        """List all active timers."""
        active = [e for e in self._timers.values() if not e.fired]
        if not active:
            return CommandResult(text="Es laufen keine Timer.")

        now = datetime.now(self._tz)
        parts = []
        for entry in sorted(active, key=lambda e: e.ends_at):
            remaining = (entry.ends_at - now).total_seconds()
            if remaining < 0:
                remaining = 0
            remaining_text = self._format_remaining(remaining)
            if entry.label:
                parts.append(f"{entry.label}: noch {remaining_text}")
            else:
                parts.append(f"Timer {entry.id}: noch {remaining_text}")

        if len(parts) == 1:
            return CommandResult(text=f"Ein Timer läuft: {parts[0]}.")
        else:
            listing = ", ".join(parts)
            return CommandResult(text=f"{len(parts)} Timer laufen: {listing}.")

    def _on_timer_fired(self, timer_id: int) -> None:
        """Called by asyncio event loop when a timer expires."""
        entry = self._timers.get(timer_id)
        if entry is None or entry.fired:
            return

        entry.fired = True
        duration_text = self._format_duration(entry.duration_seconds / 60.0)

        if entry.label:
            text = f"Timer für {entry.label} ist abgelaufen! {duration_text} sind um."
        else:
            text = f"Dein {duration_text}-Timer ist abgelaufen!"

        logger.info("[timer] Timer #%d fired: %s", timer_id, text)

        self._pending_notifications.append(
            Notification(
                text=text,
                entity="robbie",
                behaviors=["excited"],
                source="timer",
            )
        )

        # Clean up
        del self._timers[timer_id]

    async def check_notifications(self) -> list[Notification]:
        """Return and clear pending timer notifications."""
        if not self._pending_notifications:
            return []
        result = list(self._pending_notifications)
        self._pending_notifications.clear()
        return result

    def get_status(self) -> list[dict[str, Any]]:
        """Return active timers for UI display."""
        result = []
        for entry in sorted(self._timers.values(), key=lambda e: e.ends_at):
            if entry.fired:
                continue
            result.append(
                {
                    "type": "timer",
                    "id": entry.id,
                    "label": entry.label,
                    "ends_at": entry.ends_at.isoformat(),
                    "duration_seconds": entry.duration_seconds,
                }
            )
        return result

    def get_tool_description(self) -> str:
        return (
            "### Timer\n"
            "Du kannst Countdown-Timer setzen. Sage einfach:\n"
            '- "Timer 5 Minuten" — Timer auf 5 Minuten\n'
            '- "Timer 10 Minuten Nudeln" — Timer mit Label\n'
            '- "Timer 30 Sekunden" — Kurzer Timer\n'
            '- "Timer anderthalb Stunden" — Langer Timer\n'
            '- "Timer stopp" — Alle Timer abbrechen\n'
            '- "Welche Timer laufen?" — Status anzeigen\n'
            "Timer klingeln automatisch wenn sie ablaufen."
        )

    @staticmethod
    def _format_duration(minutes: float) -> str:
        """Format minutes as German duration string."""
        if minutes < 1:
            secs = int(minutes * 60)
            return f"{secs} Sekunde{'n' if secs != 1 else ''}"
        elif minutes == 1:
            return "1 Minute"
        elif minutes < 60:
            if minutes == int(minutes):
                return f"{int(minutes)} Minuten"
            return f"{minutes:.1f} Minuten"
        elif minutes == 60:
            return "1 Stunde"
        elif minutes % 60 == 0:
            hours = int(minutes // 60)
            return f"{hours} Stunde{'n' if hours != 1 else ''}"
        else:
            hours = int(minutes // 60)
            mins = int(minutes % 60)
            return f"{hours} Stunde{'n' if hours != 1 else ''} und {mins} Minute{'n' if mins != 1 else ''}"

    @staticmethod
    def _format_remaining(seconds: float) -> str:
        """Format remaining seconds as German string."""
        if seconds < 60:
            s = int(seconds)
            return f"{s} Sekunde{'n' if s != 1 else ''}"
        elif seconds < 3600:
            m = int(seconds // 60)
            s = int(seconds % 60)
            if s == 0:
                return f"{m} Minute{'n' if m != 1 else ''}"
            return f"{m} Minute{'n' if m != 1 else ''} und {s} Sekunde{'n' if s != 1 else ''}"
        else:
            h = int(seconds // 3600)
            m = int((seconds % 3600) // 60)
            if m == 0:
                return f"{h} Stunde{'n' if h != 1 else ''}"
            return f"{h} Stunde{'n' if h != 1 else ''} und {m} Minute{'n' if m != 1 else ''}"
