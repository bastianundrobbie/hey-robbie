"""Datetime MCP Server – Datum, Uhrzeit und Kalender-Informationen.

Ein MCP stdio-Server (FastMCP) der aktuelle Datums- und Zeitinformationen
bereitstellt. Gedacht für den Robbie-Familienassistenten.

Keine externen Dependencies außer stdlib + mcp. Alle Zeiten in der
konfigurierten Zeitzone (Default: Europe/Berlin).

Usage:
    python -m mcp_datetime --timezone Europe/Berlin
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

# German weekday names (Monday=0 … Sunday=6, matching datetime.weekday())
WEEKDAY_NAMES_DE = [
    "Montag",
    "Dienstag",
    "Mittwoch",
    "Donnerstag",
    "Freitag",
    "Samstag",
    "Sonntag",
]

# German month names (January=1 … December=12, index 0 unused)
MONTH_NAMES_DE = [
    "",
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]


def _german_weekday(d: date) -> str:
    """Return the German weekday name for a date."""
    return WEEKDAY_NAMES_DE[d.weekday()]


def _german_month(d: date) -> str:
    """Return the German month name for a date."""
    return MONTH_NAMES_DE[d.month]


def _days_description(delta_days: int) -> str:
    """Return a human-readable German description of a day offset from today."""
    if delta_days == 0:
        return "heute"
    elif delta_days == 1:
        return "morgen"
    elif delta_days == 2:
        return "übermorgen"
    elif delta_days == -1:
        return "gestern"
    elif delta_days == -2:
        return "vorgestern"
    elif delta_days > 0:
        return f"in {delta_days} Tagen"
    else:
        return f"vor {abs(delta_days)} Tagen"


def create_server(timezone: str = "Europe/Berlin") -> FastMCP:
    """Create and return a FastMCP server with all datetime tools registered.

    Args:
        timezone: IANA timezone name (e.g. ``Europe/Berlin``).
    """

    mcp = FastMCP("datetime")
    tz = ZoneInfo(timezone)

    @mcp.tool()
    def get_current_datetime() -> dict:
        """Return the current date and time with full calendar context.

        Includes: ISO date, 24h time, German weekday name, calendar week (KW),
        day of year, timezone name, and Unix timestamp. No parameters needed.

        Use this for questions like "Wie spät ist es?", "Welcher Tag ist heute?",
        "Welche Kalenderwoche haben wir?".
        """
        now = datetime.now(tz)
        iso_year, iso_week, iso_weekday = now.isocalendar()

        return {
            "date": now.strftime("%Y-%m-%d"),
            "time": now.strftime("%H:%M:%S"),
            "weekday": _german_weekday(now),
            "month_name": _german_month(now),
            "calendar_week": f"KW {iso_week}",
            "calendar_week_number": iso_week,
            "day_of_year": now.timetuple().tm_yday,
            "timezone": timezone,
            "unix_timestamp": int(now.timestamp()),
        }

    @mcp.tool()
    def get_date_info(date_str: str) -> dict:
        """Return calendar information about a specific date.

        Args:
            date_str: ISO date string, e.g. "2025-12-24".

        Returns weekday name (German), calendar week, day of year, and how many
        days from today (e.g. "in 162 Tagen" or "vor 3 Tagen" or "heute").

        Use this for questions like "Welcher Wochentag ist der 24. Dezember?",
        "Wie viele Tage bis Weihnachten?", "Was für ein Tag war der 1. Januar?".
        """
        try:
            target = date.fromisoformat(date_str)
        except ValueError:
            return {
                "error": f"Invalid date format: '{date_str}'. Expected ISO format YYYY-MM-DD."
            }

        today = datetime.now(tz).date()
        delta_days = (target - today).days
        iso_year, iso_week, iso_weekday = target.isocalendar()

        return {
            "date": target.isoformat(),
            "weekday": _german_weekday(target),
            "month_name": _german_month(target),
            "calendar_week": f"KW {iso_week}",
            "calendar_week_number": iso_week,
            "day_of_year": target.timetuple().tm_yday,
            "days_from_today": delta_days,
            "description": _days_description(delta_days),
        }

    @mcp.tool()
    def get_week_info(year: int, week: int) -> dict:
        """Return information about a specific ISO calendar week.

        Args:
            year: The year (e.g. 2025).
            week: ISO calendar week number (1-53).

        Returns start date (Monday), end date (Sunday), whether it's the current
        week, and all weekdays with their dates.

        Use this for questions like "Was ist KW 29?", "Wann ist Kalenderwoche 52?",
        "Welche Tage hat KW 1 2026?".
        """
        if week < 1 or week > 53:
            return {"error": f"Invalid week number: {week}. Must be 1-53."}

        try:
            monday = date.fromisocalendar(year, week, 1)
        except ValueError:
            return {
                "error": f"Invalid calendar week: KW {week} in year {year} does not exist."
            }

        sunday = monday + timedelta(days=6)
        today = datetime.now(tz).date()
        current_iso = today.isocalendar()
        is_current_week = current_iso[0] == year and current_iso[1] == week

        days = []
        for i in range(7):
            d = monday + timedelta(days=i)
            days.append(
                {
                    "date": d.isoformat(),
                    "weekday": WEEKDAY_NAMES_DE[i],
                    "is_today": d == today,
                }
            )

        return {
            "year": year,
            "calendar_week": f"KW {week}",
            "start_date": monday.isoformat(),
            "end_date": sunday.isoformat(),
            "is_current_week": is_current_week,
            "days": days,
        }

    @mcp.tool()
    def get_countdown(target_date: str, label: str = "") -> dict:
        """Calculate days until or since a target date.

        Args:
            target_date: ISO date string, e.g. "2025-09-01".
            label: Optional label for the event, e.g. "Weihnachten", "Sommerferien".

        Returns days remaining/past, weeks+days breakdown, and a human-readable
        German description.

        Use this for questions like "Wie viele Tage bis zum 1. September?",
        "Wie lange noch bis Weihnachten?", "Wie viele Tage seit Neujahr?".
        """
        try:
            target = date.fromisoformat(target_date)
        except ValueError:
            return {
                "error": f"Invalid date format: '{target_date}'. Expected ISO format YYYY-MM-DD."
            }

        today = datetime.now(tz).date()
        delta_days = (target - today).days
        abs_days = abs(delta_days)
        weeks = abs_days // 7
        remaining_days = abs_days % 7

        # Build human-readable breakdown
        parts = []
        if weeks > 0:
            parts.append(f"{weeks} Woche{'n' if weeks != 1 else ''}")
        if remaining_days > 0 or weeks == 0:
            parts.append(f"{remaining_days} Tag{'e' if remaining_days != 1 else ''}")
        breakdown = " und ".join(parts)

        # Build summary sentence
        if delta_days > 0:
            if label:
                summary = f"Noch {abs_days} Tage bis {label} ({breakdown})."
            else:
                summary = f"Noch {abs_days} Tage ({breakdown})."
        elif delta_days < 0:
            if label:
                summary = f"{label} war vor {abs_days} Tagen ({breakdown})."
            else:
                summary = f"Vor {abs_days} Tagen ({breakdown})."
        else:
            if label:
                summary = f"{label} ist heute!"
            else:
                summary = "Das ist heute!"

        result: dict = {
            "target_date": target.isoformat(),
            "target_weekday": _german_weekday(target),
            "today": today.isoformat(),
            "delta_days": delta_days,
            "abs_days": abs_days,
            "weeks": weeks,
            "remaining_days": remaining_days,
            "description": _days_description(delta_days),
            "breakdown": breakdown,
            "summary": summary,
        }
        if label:
            result["label"] = label

        return result

    @mcp.tool()
    def get_unix_timestamp() -> dict:
        """Return the current Unix timestamp (seconds since 1970-01-01 UTC).

        Simple utility for when the exact epoch time is needed. No parameters.
        """
        now = datetime.now(tz)
        return {
            "unix_timestamp": int(now.timestamp()),
            "unix_timestamp_float": now.timestamp(),
            "iso": now.isoformat(),
        }

    logger.info("Datetime MCP server created (timezone: %s, 5 tools)", timezone)
    return mcp
