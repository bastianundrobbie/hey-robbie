"""Weather MCP Server – aktuelles Wetter + 7-Tage-Vorschau für Robbie.

Ein MCP stdio-Server (FastMCP) mit Wetterdaten vom Deutschen Wetterdienst (DWD),
abgerufen über die freie Bright-Sky-JSON-API (https://api.brightsky.dev) — KEIN
API-Key nötig. Bright Sky löst Koordinaten auf die *nächstgelegene* DWD-Station
auf und liefert deren Name + Entfernung mit.

Standort-Default: der Heimatort aus ``--lat/--lon/--place`` (siehe
plugins.toml, Werte aus [mcp_vars] in config.toml). Über das
``location``-Argument der Tools kann ein anderer Ort abgefragt werden (per
Open-Meteo-Geocoding aufgelöst).

Keine externen Dependencies außer stdlib + mcp (HTTP via urllib).

Usage:
    python -m mcp_weather --lat 52.52 --lon 13.405 --place "Musterstadt"
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from mcp.server.fastmcp import FastMCP  # type: ignore[import-not-found]

logger = logging.getLogger(__name__)

# ── Home location fallback (PLACEHOLDER — set the real one in config.toml
#    [mcp_vars]; these defaults only apply when no --lat/--lon are passed) ──
DEFAULT_LAT = 52.52
DEFAULT_LON = 13.405
DEFAULT_NAME = "Musterstadt"

# German IANA timezone.
TIMEZONE = "Europe/Berlin"
_TZ = ZoneInfo(TIMEZONE)

BRIGHTSKY_BASE = "https://api.brightsky.dev"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_HTTP_TIMEOUT = 12
_USER_AGENT = "robbie-weather/1.0"

# German weekday names (Monday=0 … Sunday=6, matching date.weekday()).
WEEKDAY_NAMES_DE = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]

# Bright Sky `icon` (day/night suffix stripped) → German text. `icon` carries the
# cloudiness nuance that the coarser `condition` lacks, so it is preferred.
ICON_DE = {
    "clear": "klar",
    "partly-cloudy": "teils bewölkt",
    "cloudy": "bewölkt",
    "fog": "neblig",
    "wind": "windig",
    "rain": "Regen",
    "sleet": "Schneeregen",
    "snow": "Schnee",
    "hail": "Hagel",
    "thunderstorm": "Gewitter",
}

# Fallback: Bright Sky `condition` → German text (used when `icon` is missing).
CONDITION_DE = {
    "dry": "trocken",
    "fog": "neblig",
    "rain": "Regen",
    "sleet": "Schneeregen",
    "snow": "Schnee",
    "hail": "Hagel",
    "thunderstorm": "Gewitter",
}


def _http_get_json(url: str, timeout: int = _HTTP_TIMEOUT) -> dict:
    """GET a URL and parse the JSON body. Raises on network/HTTP/parse error."""
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def describe(condition: str | None, icon: str | None) -> str:
    """Map Bright Sky condition/icon to a short German description."""
    if icon:
        base = icon.replace("-day", "").replace("-night", "")
        if base in ICON_DE:
            return ICON_DE[base]
    if condition and condition in CONDITION_DE:
        return CONDITION_DE[condition]
    return "unbekannt"


def _geocode(name: str) -> tuple[float, float, str] | None:
    """Resolve a place name to (lat, lon, resolved_name) via Open-Meteo. None on miss."""
    q = urllib.parse.urlencode(
        {"name": name, "count": 1, "language": "de", "format": "json"}
    )
    try:
        data = _http_get_json(f"{GEOCODE_URL}?{q}")
    except Exception as exc:
        logger.warning("geocode failed for %r: %s", name, exc)
        return None
    results = data.get("results") or []
    if not results:
        return None
    r = results[0]
    label = r.get("name", name)
    admin = r.get("admin1")
    resolved = f"{label}, {admin}" if admin and admin != label else label
    return float(r["latitude"]), float(r["longitude"]), resolved


def _resolve_location(
    location: str, home: tuple[float, float, str]
) -> tuple[float, float, str] | None:
    """Empty location → home; otherwise geocode. None if a named place is not found."""
    if not location or not location.strip():
        return home
    return _geocode(location.strip())


def _station_from_sources(payload: dict) -> tuple[str | None, float | None]:
    """Nearest station name + distance (km) from a Bright Sky payload."""
    sources = payload.get("sources") or []
    if not sources:
        return None, None
    s = sources[0]
    dist_m = s.get("distance")
    dist_km = round(dist_m / 1000.0, 1) if isinstance(dist_m, (int, float)) else None
    return s.get("station_name"), dist_km


def _fetch_current(lat: float, lon: float) -> dict:
    """Current conditions from the nearest DWD observation station."""
    q = urllib.parse.urlencode({"lat": lat, "lon": lon, "tz": TIMEZONE})
    data = _http_get_json(f"{BRIGHTSKY_BASE}/current_weather?{q}")
    w = data.get("weather") or {}
    station, dist_km = _station_from_sources(data)
    temp = w.get("temperature")
    wind = w.get("wind_speed_60")
    gust = w.get("wind_gust_speed_60")
    return {
        "temperature_c": round(temp, 1) if isinstance(temp, (int, float)) else None,
        "description": describe(w.get("condition"), w.get("icon")),
        "condition": w.get("condition"),
        "icon": w.get("icon"),
        "wind_kmh": round(wind) if isinstance(wind, (int, float)) else None,
        "wind_gust_kmh": round(gust) if isinstance(gust, (int, float)) else None,
        "humidity_pct": w.get("relative_humidity"),
        "precipitation_mm": w.get("precipitation_60"),
        "station": station,
        "station_distance_km": dist_km,
        "observed_at": w.get("timestamp"),
    }


def _pick_representative(records: list[dict]) -> dict:
    """The hourly record closest to 13:00 local — stands in for the day's look."""
    def hour(rec: dict) -> int:
        ts = rec.get("timestamp", "")
        try:
            return int(ts[11:13])
        except (ValueError, IndexError):
            return 12
    return min(records, key=lambda r: abs(hour(r) - 13))


def _summarize_day(day: str, records: list[dict], today: str) -> dict:
    """Aggregate one local day's hourly records into a daily summary."""
    temps = [r["temperature"] for r in records if r.get("temperature") is not None]
    precip = [r["precipitation"] for r in records if r.get("precipitation") is not None]
    probs = [
        r["precipitation_probability"]
        for r in records
        if r.get("precipitation_probability") is not None
    ]
    winds = [r["wind_speed"] for r in records if r.get("wind_speed") is not None]
    rep = _pick_representative(records)
    d = datetime.strptime(day, "%Y-%m-%d").date()
    return {
        "date": day,
        "weekday": WEEKDAY_NAMES_DE[d.weekday()],
        "is_today": day == today,
        "description": describe(rep.get("condition"), rep.get("icon")),
        "icon": rep.get("icon"),
        "temp_min_c": round(min(temps)) if temps else None,
        "temp_max_c": round(max(temps)) if temps else None,
        "precipitation_mm": round(sum(precip), 1) if precip else 0.0,
        "precipitation_probability_pct": max(probs) if probs else None,
        "wind_max_kmh": round(max(winds)) if winds else None,
    }


def _fetch_forecast(lat: float, lon: float, days: int) -> dict:
    """Daily forecast (aggregated from hourly) from the nearest DWD MOSMIX station."""
    days = max(1, min(7, days))
    today_d = datetime.now(_TZ).date()
    last_d = today_d + timedelta(days=days - 1)
    q = urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "tz": TIMEZONE,
         "date": today_d.isoformat(), "last_date": last_d.isoformat()}
    )
    data = _http_get_json(f"{BRIGHTSKY_BASE}/weather?{q}")
    records = data.get("weather") or []
    if not records:
        return {"error": "Keine Vorhersagedaten verfügbar."}

    # Group hourly records by local calendar day. Timestamps are already local
    # (tz=Europe/Berlin requested), so the leading YYYY-MM-DD is the local date.
    by_day: dict[str, list[dict]] = {}
    for rec in records:
        ts = rec.get("timestamp", "")
        day = ts[:10]
        if day:
            by_day.setdefault(day, []).append(rec)

    today = today_d.isoformat()
    day_summaries = [
        _summarize_day(day, by_day[day], today)
        for day in sorted(by_day)
    ][:days]

    station, dist_km = _station_from_sources(data)
    return {"station": station, "station_distance_km": dist_km, "days": day_summaries}


def create_server(
    home_lat: float = DEFAULT_LAT,
    home_lon: float = DEFAULT_LON,
    home_name: str = DEFAULT_NAME,
) -> FastMCP:
    """Create and return a FastMCP server with the weather tools registered."""

    mcp = FastMCP("weather")
    home = (home_lat, home_lon, home_name)

    @mcp.tool()
    def current_weather(location: str = "") -> dict:
        """Aktuelles Wetter von der nächstgelegenen DWD-Wetterstation.

        Args:
            location: Ort. Leer lassen für zuhause. Sonst ein
                Ortsname (z. B. "Hamburg"), der automatisch aufgelöst wird.

        Liefert Temperatur, Wetterlage (deutsch), Wind/Böen, Luftfeuchte und den
        Namen + die Entfernung der genutzten Station.

        Nutze dies für "Wie ist das Wetter?", "Wie warm ist es?", "Regnet es
        gerade?", "Muss ich eine Jacke anziehen?".
        """
        loc = _resolve_location(location, home)
        if loc is None:
            return {"error": f"Ort '{location}' nicht gefunden."}
        lat, lon, name = loc
        try:
            result = _fetch_current(lat, lon)
        except Exception as exc:
            logger.warning("current_weather failed: %s", exc)
            return {"error": "Wetterdaten gerade nicht abrufbar."}
        result["location"] = name
        return result

    @mcp.tool()
    def forecast(location: str = "", days: int = 7) -> dict:
        """Wettervorschau für die nächsten Tage (bis zu 7) von der nächsten DWD-Station.

        Args:
            location: Ort. Leer lassen für zuhause. Sonst ein
                Ortsname, der automatisch aufgelöst wird.
            days: Anzahl Tage (1–7, Standard 7). Tag 1 ist heute.

        Liefert pro Tag: Wochentag, Datum, Wetterlage (deutsch), Min-/Max-
        Temperatur, Niederschlagsmenge + -wahrscheinlichkeit und Maximalwind —
        plus Name + Entfernung der genutzten Station.

        Nutze dies für "Wie wird das Wetter diese Woche?", "Wie wird das Wetter
        morgen?", "Regnet es am Wochenende?", "Brauche ich am Freitag einen
        Regenschirm?".
        """
        loc = _resolve_location(location, home)
        if loc is None:
            return {"error": f"Ort '{location}' nicht gefunden."}
        lat, lon, name = loc
        try:
            result = _fetch_forecast(lat, lon, days)
        except Exception as exc:
            logger.warning("forecast failed: %s", exc)
            return {"error": "Wettervorhersage gerade nicht abrufbar."}
        result["location"] = name
        return result

    logger.info(
        "Weather MCP server created (home: %s @ %.3f,%.3f, 2 tools, DWD/Bright Sky)",
        home_name, home_lat, home_lon,
    )
    return mcp
