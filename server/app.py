"""Robbie server — the brain behind the speech station.

Starts one persistent ``ClaudeSDKClient`` per entity (normally just
``robbie``) and exposes it to the Pi station over WebSockets:

    python -m server.app
    python -m server.app --port 8422 --entities server/config/config.toml

API::

    WS         /voice/duplex         the Pi station's persistent uplink
                                     (mic in, wakeword/STT/LLM/TTS server-side)
    WS         /voice/stream         one turn from a ready transcript (tests)
    WS         /api/stt/stream       Deepgram streaming STT proxy (tests)
    POST       /api/stt              Deepgram batch STT proxy (tests)
    GET        /api/health           server, entity and station status
    GET        /entities             entity list (voice/TTS settings)
    GET/POST   /api/entities/{n}/settings   model + effort (persisted)
    POST       /api/entities/{n}/reconnect  restart the entity's SDK process
    GET        /api/entities/{n}/context    context-window usage
    GET        /api/voices           Cartesia voice catalogue (German voices)
    GET        /api/voices/{id}/preview     voice sample audio
    POST       /api/notify           speak a text at the station (test hook)
    POST       /api/capabilities/command    run the deterministic fast-path
    POST       /api/reload           hot-reload the config files
    POST       /api/restart          restart the server process
    GET        /api/env              which API keys are set (no values)

Authentication for the LLM: ``ANTHROPIC_API_KEY`` in the environment (see
``.env.example``). STT needs ``DEEPGRAM_API_KEY``, TTS ``CARTESIA_API_KEY``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import aiohttp
import tomllib
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response

from server.entities import (
    EntityConfig,
    EntityManager,
    SDKWrapper,
    load_entities,
)
from server.tts import TTSManager

# ── Logging ──────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("robbie-server")


# =====================================================================
#  Behavior Tag Extraction
# =====================================================================

_BEHAVIOR_TAG_RE = re.compile(r"\[ROBBIE:(\w+)\]")


def _extract_behavior_tags(text: str) -> tuple[str, list[str]]:
    """Extract [ROBBIE:xxx] tags from text, return (clean_text, behavior_names)."""
    behaviors = [m.lower() for m in _BEHAVIOR_TAG_RE.findall(text)]
    clean = _BEHAVIOR_TAG_RE.sub("", text).strip()
    return clean, behaviors


# =====================================================================
#  FastAPI Application
# =====================================================================

# Globals — set in lifespan
entity_manager: EntityManager | None = None
_tts_manager: TTSManager | None = None
_cli_args: argparse.Namespace | None = None
_config: dict = {}

# Voice WebSocket — singleton (only one voice client at a time)
_voice_ws: WebSocket | None = None
_voice_turn_task: asyncio.Task[None] | None = None
_voice_cancel_event: asyncio.Event = asyncio.Event()

# Shared aiohttp session for Deepgram proxy endpoints (initialized in lifespan)
_proxy_session: aiohttp.ClientSession | None = None

# Deepgram API key, resolved once at startup
_deepgram_api_key: str = ""

# Deepgram regional endpoint (same key works on all regions). EU
# (api.eu.deepgram.com) would save ~0.3 s TLS setup per turn socket, but
# flux-general-multi answers INTERNAL_SERVER_ERROR there (verified
# 2026-08-22; nova-3 works) — US stays the default until EU serves Flux.
# Mirrored in duplex.py (no cross-import: module-instance trap).
_DEEPGRAM_HOST = os.environ.get("DEEPGRAM_API_HOST", "api.deepgram.com")

# Capabilities (timer) — proactive notifications reach the station via
# the duplex link's notify consumer (server/duplex.py). Set in lifespan.
capability_manager = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """App-Lifespan: Entities laden und verbinden, beim Shutdown trennen."""
    global entity_manager, _tts_manager, _proxy_session, _deepgram_api_key

    args = _cli_args
    assert args is not None

    # Resolve Deepgram API key once (used by STT/TTS proxy endpoints)
    _deepgram_api_key = os.environ.get("DEEPGRAM_API_KEY", "")
    if _deepgram_api_key:
        logger.info("[proxy] DEEPGRAM_API_KEY loaded")
    else:
        logger.warning("[proxy] DEEPGRAM_API_KEY not set — STT proxy disabled")

    # Shared aiohttp session for STT proxy endpoints
    _proxy_session = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=30),
        connector=aiohttp.TCPConnector(keepalive_timeout=300),
    )

    # TTS Manager
    _tts_manager = TTSManager()
    _tts_manager.configure(
        cartesia_key=os.environ.get("CARTESIA_API_KEY", ""),
    )

    # Load entities
    default_entity, configs, mcp_registry = load_entities(args.entities)

    entity_manager = EntityManager(
        default_entity=default_entity,
        configs=configs,
        mcp_registry=mcp_registry,
        config=_config,
    )
    await entity_manager.startup()

    # Connect streaming TTS pools for entities that use them
    await _tts_manager.connect_cartesia_pools(configs)

    # Capabilities: deterministic voice commands that bypass the LLM (timer)
    # plus proactive notifications. Delivery needs a connected duplex station;
    # the queue buffers otherwise.
    global capability_manager
    from server.capabilities import CapabilityManager
    from server.capabilities.timer import TimerCapability

    capability_manager = CapabilityManager()
    capability_manager.register(TimerCapability())
    await capability_manager.startup()

    yield

    # Shutdown
    await capability_manager.shutdown()
    capability_manager = None
    await _tts_manager.disconnect_cartesia_pools()
    await entity_manager.shutdown()
    await _proxy_session.close()
    _proxy_session = None
    _tts_manager = None


app = FastAPI(
    title="Robbie Server",
    description="Voice assistant brain: Claude (Anthropic) + Deepgram + Cartesia",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
async def root():
    """Plain liveness answer (there is no web dashboard in this edition)."""
    return {"service": "robbie-server", "docs": "/docs", "health": "/api/health"}


# =====================================================================
#  Health & Entity Endpoints
# =====================================================================


@app.get("/api/health")
async def health():
    """Server- und Entity-Status."""
    if not entity_manager:
        return JSONResponse({"status": "starting"}, status_code=503)

    entities_status = {}
    for name, cfg in entity_manager.configs.items():
        try:
            wrapper, _ = entity_manager.get(name)
            entities_status[name] = {
                "connected": wrapper.connected,
                "model": wrapper.model,
                "display_name": cfg.display_name,
            }
        except KeyError:
            entities_status[name] = {"connected": False, "error": "not found"}

    any_connected = any(e.get("connected") for e in entities_status.values())

    # Duplex station: the Pi connects via /voice/duplex, not the
    # legacy /voice/stream path. Lazy import like /api/notify so a missing
    # voice stack degrades this field only. voice_connected stays for
    # backwards compatibility (legacy /voice/stream clients).
    try:
        from server import duplex as duplex_mod

        station = {
            "connected": duplex_mod._duplex_ws is not None,
            "name": duplex_mod._station_name,
        }
    except Exception:
        station = {"connected": False, "name": None}

    return {
        "status": "ok" if any_connected else "degraded",
        "default_entity": entity_manager.default_entity,
        "entities": entities_status,
        "failed": entity_manager.failed,
        "voice_connected": _voice_ws is not None,
        "station": station,
    }


@app.get("/entities")
async def get_entities():
    """Entity-Liste für Voice-Client Bootstrapping.

    Format::

        {
          "entities": [
            {
              "id": "robbie",
              "display_name": "Robbie",
              "status": "active",
              "wakeword": "hey_robbie",
              "tts_provider": "cartesia",
              "tts_model": "sonic-3",
              "tts_voice_id": "9b4d08b6-0494-4301-ab92-9150f4ee2718",
              "tts_gain": 0.0
            }
          ],
          "default": "robbie"
        }
    """
    if not entity_manager:
        return JSONResponse({"entities": [], "default": ""}, status_code=503)

    failed = entity_manager.failed
    entities = []
    for name, cfg in entity_manager.configs.items():
        d = cfg.voice_dict()
        if name in failed:
            d["status"] = "failed"
            d["error"] = failed[name]
        entities.append(d)

    return JSONResponse(
        {
            "entities": entities,
            "default": entity_manager.default_entity,
        }
    )


# =====================================================================
#  Management API — Entities, Reload, Restart, Env
# =====================================================================


@app.post("/api/entities/{name}/reconnect")
async def reconnect_entity(name: str):
    """Disconnect and reconnect a single entity's SDK subprocess."""
    if not entity_manager:
        return JSONResponse(
            {"ok": False, "error": "Server not initialized"}, status_code=503
        )

    try:
        wrapper, cfg = entity_manager.get(name)
    except KeyError:
        return JSONResponse(
            {"ok": False, "error": f"Unbekannte Entity: {name}"},
            status_code=404,
        )

    try:
        logger.info("[web] Reconnecting entity: %s", name)
        await wrapper.disconnect()
        await wrapper.connect()
        return {"ok": True, "message": f"Entity '{cfg.display_name}' reconnected"}
    except Exception as exc:
        logger.error("[web] Reconnect failed for %s: %s", name, exc)
        return JSONResponse(
            {"ok": False, "error": f"Reconnect fehlgeschlagen: {exc}"},
            status_code=500,
        )


# Allowed values for the per-entity settings endpoint. Models are SDK short
# aliases (the SDK maps them to the concrete claude-* ids); "" effort means
# the SDK default.
_VALID_MODELS = ("opus", "sonnet", "haiku", "fable")
_VALID_EFFORTS = ("low", "medium", "high", "max")


def _persist_entity_settings(
    name: str,
    *,
    model: str | None = None,
    effort: str | None = None,
) -> None:
    """Surgically rewrite the model/effort lines in config.toml.

    Anchored, single-match replacement (the keys are unique in the file —
    only one entity, and ``^model`` does not match ``tts_model``). Backs up
    to ``.toml.bak`` and validates the result before writing.
    """
    toml_path = Path(_cli_args.entities)
    content = toml_path.read_text(encoding="utf-8")
    original = content

    if model is not None:
        content, n = re.subn(
            r"(?m)^(model\s*=\s*).*$", rf'\g<1>"{model}"', content
        )
        if n != 1:
            raise ValueError(f"model-Zeile nicht eindeutig (Treffer={n})")

    if effort is not None:
        content, n = re.subn(
            r"(?m)^(effort\s*=\s*).*$", rf'\g<1>"{effort}"', content
        )
        if n == 0:
            # No effort key yet — insert it right after the model line.
            content, n2 = re.subn(
                r"(?m)^(model\s*=\s*.*)$",
                rf'\g<1>\neffort = "{effort}"',
                content,
            )
            if n2 != 1:
                raise ValueError("effort konnte nicht eingefügt werden")
        elif n != 1:
            raise ValueError(f"effort-Zeile nicht eindeutig (Treffer={n})")

    # Validate before touching disk
    tomllib.loads(content)
    # Best-effort backup: if the config dir is not writable for a sibling
    # ``.bak``, don't let that block the save — the in-place write below
    # preserves the mounted file's inode.
    try:
        toml_path.with_suffix(".toml.bak").write_text(original, encoding="utf-8")
    except OSError as exc:
        logger.warning("[api] config.toml.bak backup skipped: %s", exc)
    toml_path.write_text(content, encoding="utf-8")
    logger.info(
        "[api] %s aktualisiert: %s model=%s effort=%s",
        toml_path.name,
        name,
        model,
        effort,
    )


@app.get("/api/entities/{name}/settings")
async def get_entity_settings(name: str):
    """Return the current model/effort plus the allowed choices."""
    if not entity_manager:
        return JSONResponse({"error": "Server not initialized"}, status_code=503)
    try:
        _, cfg = entity_manager.get(name)
    except KeyError:
        return JSONResponse({"error": f"Unbekannte Entity: {name}"}, status_code=404)
    return {
        "model": cfg.model,
        "effort": cfg.effort,
        "valid_models": list(_VALID_MODELS),
        "valid_efforts": list(_VALID_EFFORTS),
    }


@app.post("/api/entities/{name}/settings")
async def set_entity_settings(name: str, request: Request):
    """Update model and/or effort: persist to entities.toml, rebuild the SDK
    options and reconnect so the running session adopts the change.

    Reconnect discards the entity's in-session working memory (fresh SDK
    process) — that is inherent to switching model mid-conversation.
    """
    if not entity_manager:
        return JSONResponse(
            {"ok": False, "error": "Server not initialized"}, status_code=503
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"ok": False, "error": "Invalid JSON body"}, status_code=400
        )

    model = body.get("model")
    effort = body.get("effort")
    if model is not None and model not in _VALID_MODELS:
        return JSONResponse(
            {"ok": False, "error": f"Ungültiges Modell: {model}"}, status_code=400
        )
    if effort is not None and effort not in _VALID_EFFORTS:
        return JSONResponse(
            {"ok": False, "error": f"Ungültiger Effort: {effort}"}, status_code=400
        )
    if model is None and effort is None:
        return JSONResponse(
            {"ok": False, "error": "Nichts zu ändern"}, status_code=400
        )

    try:
        wrapper, cfg = entity_manager.get(name)
    except KeyError:
        return JSONResponse(
            {"ok": False, "error": f"Unbekannte Entity: {name}"}, status_code=404
        )

    # 1. Persist first — if writing the file fails we change nothing live.
    try:
        _persist_entity_settings(name, model=model, effort=effort)
    except Exception as exc:
        return JSONResponse(
            {"ok": False, "error": f"Speichern fehlgeschlagen: {exc}"},
            status_code=500,
        )

    # 2. Apply in memory + rebuild SDK options.
    entity_manager.apply_settings(name, model=model, effort=effort)

    # 3. Reconnect so the live process uses the new model/effort.
    try:
        await wrapper.disconnect()
        await wrapper.connect()
    except Exception as exc:
        logger.error("[web] settings reconnect failed for %s: %s", name, exc)
        return JSONResponse(
            {"ok": False, "error": f"Reconnect fehlgeschlagen: {exc}"},
            status_code=500,
        )

    logger.info(
        "[web] %s settings: model=%s effort=%s", name, cfg.model, cfg.effort
    )
    return {"ok": True, "model": cfg.model, "effort": cfg.effort}


# =====================================================================
#  Voice Catalog (Cartesia) — list + preview (pick a tts_voice_id)
# =====================================================================

_CARTESIA_API = "https://api.cartesia.ai"
_CARTESIA_VERSION = "2026-03-01"
# id -> preview_file_url, populated by list_voices (auth-gated URLs stay server-side)
_voice_preview_cache: dict[str, str] = {}


def _cartesia_headers() -> dict[str, str] | None:
    """Auth headers for the Cartesia REST API, or None if no key is set."""
    key = os.environ.get("CARTESIA_API_KEY", "")
    if not key:
        return None
    return {"Authorization": f"Bearer {key}", "Cartesia-Version": _CARTESIA_VERSION}


async def _fetch_voice(voice_id: str) -> dict[str, Any] | None:
    """Fetch a single Cartesia voice (with preview URL expanded), or None."""
    headers = _cartesia_headers()
    if headers is None or _proxy_session is None:
        return None
    try:
        async with _proxy_session.get(
            f"{_CARTESIA_API}/voices/{voice_id}",
            params=[("expand[]", "preview_file_url")],
            headers=headers,
        ) as resp:
            if resp.status != 200:
                return None
            return await resp.json()
    except Exception:
        return None


@app.get("/api/voices")
async def list_voices(language: str = "de"):
    """Proxy Cartesia's voice catalogue, filtered by language (default ``de``).

    Returns a slimmed list; the API key and the auth-gated preview URLs stay
    server-side. Preview audio is served via ``/api/voices/{id}/preview``.
    """
    headers = _cartesia_headers()
    if headers is None:
        return JSONResponse(
            {"error": "CARTESIA_API_KEY nicht gesetzt"}, status_code=503
        )
    assert _proxy_session is not None
    try:
        async with _proxy_session.get(
            f"{_CARTESIA_API}/voices",
            params=[
                ("language", language),
                ("limit", "100"),
                ("expand[]", "preview_file_url"),
            ],
            headers=headers,
        ) as resp:
            if resp.status != 200:
                detail = (await resp.text())[:300]
                return JSONResponse(
                    {"error": f"Cartesia {resp.status}", "detail": detail},
                    status_code=502,
                )
            data = await resp.json()
    except Exception as exc:
        return JSONResponse(
            {"error": f"Cartesia unerreichbar: {exc}"}, status_code=502
        )

    voices = []
    for v in data.get("data", []):
        vid = v.get("id")
        preview = v.get("preview_file_url")
        if vid and preview:
            _voice_preview_cache[vid] = preview
        voices.append(
            {
                "id": vid,
                "name": v.get("name") or vid,
                "gender": v.get("gender"),
                "country": v.get("country"),
                "is_pro": bool(v.get("is_pro")),
                "has_preview": bool(preview),
            }
        )
    return {"count": len(voices), "voices": voices, "language": language}


@app.get("/api/voices/{voice_id}/preview")
async def voice_preview(voice_id: str):
    """Stream a voice's preview audio (free — no TTS credits used)."""
    headers = _cartesia_headers()
    if headers is None:
        return JSONResponse(
            {"error": "CARTESIA_API_KEY nicht gesetzt"}, status_code=503
        )
    url = _voice_preview_cache.get(voice_id)
    if not url:
        voice = await _fetch_voice(voice_id)
        url = voice.get("preview_file_url") if voice else None
        if url:
            _voice_preview_cache[voice_id] = url
    if not url:
        return JSONResponse(
            {"error": "Kein Preview für diese Stimme"}, status_code=404
        )
    assert _proxy_session is not None
    try:
        async with _proxy_session.get(url, headers=headers) as resp:
            if resp.status != 200:
                return JSONResponse(
                    {"error": f"Preview {resp.status}"}, status_code=502
                )
            content = await resp.read()
            media = resp.headers.get("Content-Type", "audio/mpeg")
    except Exception as exc:
        return JSONResponse(
            {"error": f"Preview-Fetch fehlgeschlagen: {exc}"}, status_code=502
        )
    return Response(content=content, media_type=media)


@app.get("/api/entities/{name}/context")
async def get_entity_context(name: str):
    """Get context window usage for an entity."""
    if not entity_manager:
        return JSONResponse({"error": "Server not initialized"}, status_code=503)

    try:
        wrapper, _ = entity_manager.get(name)
    except KeyError:
        return JSONResponse({"error": f"Unbekannte Entity: {name}"}, status_code=404)

    if not wrapper.connected:
        return JSONResponse({"error": "Entity nicht verbunden"}, status_code=503)

    usage = await wrapper.get_context_usage()
    if usage is None:
        return JSONResponse(
            {"error": "Context-Usage nicht verfügbar"},
            status_code=503,
        )

    return usage


@app.post("/api/reload")
async def reload_config():
    """Hot-reload: Re-read entities.toml, disconnect all, reconnect all."""
    global entity_manager

    args = _cli_args
    if not args or not entity_manager or not _tts_manager:
        return JSONResponse(
            {"ok": False, "error": "Server not initialized"}, status_code=503
        )

    try:
        # 1. Parse new config (validates before any disconnect)
        new_default, new_configs, new_mcp_registry = load_entities(args.entities)
        logger.info("[api] Neue Konfiguration geladen: %d Entities", len(new_configs))

        # 2. Disconnect old TTS pools
        await _tts_manager.disconnect_cartesia_pools()

        # 3. Disconnect old entities
        await entity_manager.shutdown()

        # 4. Create new EntityManager
        entity_manager = EntityManager(
            default_entity=new_default,
            configs=new_configs,
            mcp_registry=new_mcp_registry,
            config=_config,
        )

        # 5. Connect new entities
        await entity_manager.startup()

        # 6. Connect new TTS pools
        await _tts_manager.connect_cartesia_pools(new_configs)

        reloaded = [c.name for c in new_configs]
        logger.info("[web] Reload abgeschlossen: %s", reloaded)

        return {
            "ok": True,
            "message": f"{len(reloaded)} Entity(s) neu geladen",
            "reloaded_entities": reloaded,
        }

    except Exception as exc:
        logger.error("[web] Reload fehlgeschlagen: %s", exc, exc_info=True)
        return JSONResponse(
            {"ok": False, "error": f"Reload fehlgeschlagen: {exc}"},
            status_code=500,
        )


@app.post("/api/restart")
async def restart_server():
    """Full server process restart via os.execv."""
    logger.warning("[web] Server-Neustart angefordert!")

    async def _do_restart():
        """Delayed restart: give time for the HTTP response to be sent."""
        await asyncio.sleep(0.5)
        logger.info("[web] Starte Server-Prozess neu...")
        # Re-exec the current process
        os.execv(sys.executable, [sys.executable, "-m", "server.app"] + sys.argv[1:])

    asyncio.create_task(_do_restart())
    return {"ok": True, "message": "Server wird neu gestartet..."}


@app.get("/api/env")
async def get_env_vars():
    """Show which environment variables are set (no values for security).

    All three keys must be set for a full turn: ANTHROPIC (LLM), DEEPGRAM
    (STT), CARTESIA (TTS).
    """
    env_keys = [
        "ANTHROPIC_API_KEY",
        "DEEPGRAM_API_KEY",
        "CARTESIA_API_KEY",
    ]
    return {"vars": {key: bool(os.environ.get(key, "")) for key in env_keys}}


# =====================================================================
#  STT Proxy (for mobile clients that can't call Deepgram directly)
# =====================================================================


@app.websocket("/api/stt/stream")
async def stt_stream_proxy(ws: WebSocket):
    """Streaming STT proxy — bridges mobile client to Deepgram WebSocket.

    The mobile client sends raw PCM16 audio chunks (16kHz mono) and
    receives interim/final transcripts.  This proxy exists because
    Android's Dart WebSocket implementation mangles Authorization headers.

    Client → Server: binary PCM16 frames
    Server → Client: JSON transcript events (forwarded from Deepgram)
    """
    if not _deepgram_api_key:
        await ws.accept()
        await ws.send_json({"error": "DEEPGRAM_API_KEY not set"})
        await ws.close(code=1008)
        return

    await ws.accept()

    # Build Deepgram streaming URL from query params
    model = ws.query_params.get("model", "nova-3")
    language = ws.query_params.get("language", "de")
    keywords = ws.query_params.get("keywords", "")
    # Silence (ms) Deepgram waits before emitting speech_final. Its default
    # (~10ms) fires on the briefest pause and cuts speakers off mid-thought
    # (the client ends the turn on the first speech_final). Client-tunable,
    # patient default.
    endpointing = ws.query_params.get("endpointing", "1000")
    if not endpointing.isdigit():
        endpointing = "1000"

    dg_url = (
        f"wss://{_DEEPGRAM_HOST}/v1/listen"
        f"?model={model}&language={language}"
        f"&encoding=linear16&sample_rate=16000&channels=1"
        f"&punctuate=true&interim_results=true"
        f"&utterance_end_ms=1000&vad_events=true"
        f"&endpointing={endpointing}"
    )
    if keywords:
        from urllib.parse import quote

        # Nova-3 uses "keyterm" instead of "keywords"
        param_name = "keyterm" if "nova-3" in model else "keywords"
        for kw in keywords.split(","):
            kw = kw.strip()
            if kw:
                dg_url += f"&{param_name}={quote(kw)}"

    try:
        assert _proxy_session is not None
        async with _proxy_session.ws_connect(
            dg_url,
            headers={"Authorization": f"Token {_deepgram_api_key}"},
        ) as dg_ws:
            logger.info("[STT stream] Connected to Deepgram")

            async def _forward_audio():
                """Forward audio from mobile client to Deepgram."""
                try:
                    while True:
                        data = await ws.receive()
                        if data["type"] == "websocket.disconnect":
                            break
                        if "bytes" in data and data["bytes"]:
                            await dg_ws.send_bytes(data["bytes"])
                        elif "text" in data and data["text"]:
                            # Client sends JSON control messages (e.g. CloseStream)
                            await dg_ws.send_str(data["text"])
                except WebSocketDisconnect:
                    pass
                except Exception as e:
                    logger.debug("[STT stream] Forward error: %s", e)
                finally:
                    # Signal Deepgram to finalize
                    try:
                        await dg_ws.send_str('{"type": "CloseStream"}')
                    except Exception:
                        pass

            async def _forward_transcripts():
                """Forward transcripts from Deepgram to mobile client."""
                try:
                    async for msg in dg_ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            await ws.send_text(msg.data)
                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            break
                except Exception as e:
                    logger.debug("[STT stream] Transcript forward error: %s", e)

            # Run both directions concurrently
            await asyncio.gather(
                _forward_audio(),
                _forward_transcripts(),
                return_exceptions=True,
            )

            logger.info("[STT stream] Disconnected")

    except Exception as e:
        logger.error("[STT stream] Deepgram connect error: %s", e)
        try:
            await ws.send_json({"error": f"Deepgram connect failed: {e}"})
        except Exception:
            pass

    try:
        await ws.close()
    except Exception:
        pass


@app.post("/api/stt")
async def stt_proxy(request: Request):
    """Proxy STT request to Deepgram.

    Accepts raw PCM16 audio (16kHz mono) and returns the transcript.
    Mobile clients use this instead of calling Deepgram directly
    (Android's Dart runtime mangles Authorization headers).

    Request::

        POST /api/stt
        Content-Type: audio/raw
        Body: raw PCM16 bytes (16kHz mono)

    Optional query params: model, language, smart_format

    Response::

        {"transcript": "Hallo Welt", "duration_ms": 342}
    """
    audio_bytes = await request.body()
    if not audio_bytes:
        return JSONResponse({"error": "No audio data"}, status_code=400)

    if not _deepgram_api_key:
        return JSONResponse({"error": "DEEPGRAM_API_KEY not set"}, status_code=500)

    # Build Deepgram URL with query params
    model = request.query_params.get("model", "nova-3")
    language = request.query_params.get("language", "de")
    smart_format = request.query_params.get("smart_format", "true")
    dg_url = (
        f"https://{_DEEPGRAM_HOST}/v1/listen"
        f"?model={model}&language={language}&smart_format={smart_format}"
        f"&encoding=linear16&sample_rate=16000&channels=1"
    )

    t0 = time.monotonic()
    logger.info(
        f"[STT proxy] {len(audio_bytes)} bytes ({len(audio_bytes) / 32000:.1f}s audio)"
    )

    try:
        assert _proxy_session is not None
        async with _proxy_session.post(
            dg_url,
            headers={
                "Authorization": f"Token {_deepgram_api_key}",
                "Content-Type": "audio/raw",
            },
            data=audio_bytes,
        ) as resp:
            duration_ms = int((time.monotonic() - t0) * 1000)

            if resp.status != 200:
                body = await resp.text()
                logger.warning(f"[STT proxy] Deepgram HTTP {resp.status}: {body[:200]}")
                return JSONResponse(
                    {"error": f"Deepgram HTTP {resp.status}", "detail": body[:500]},
                    status_code=502,
                )

            data = await resp.json()
            channels = data.get("results", {}).get("channels", [])
            transcript = ""
            if channels:
                alts = channels[0].get("alternatives", [])
                if alts:
                    transcript = alts[0].get("transcript", "")

            logger.info(f'[STT proxy] "{transcript}" ({duration_ms}ms)')
            return {"transcript": transcript, "duration_ms": duration_ms}

    except Exception as e:
        logger.error(f"[STT proxy] Error: {e}")
        return JSONResponse({"error": str(e)}, status_code=502)


# =====================================================================
#  Voice WebSocket  (/voice/stream)  — one turn from a transcript
# =====================================================================


async def _voice_turn_runner(
    ws: WebSocket,
    sdk: SDKWrapper,
    entity_cfg: EntityConfig,
    turn_id: str,
    transcript: str,
    cancel_event: asyncio.Event,
    speaker: str = "",
) -> None:
    """Background task: Run a turn on the SDK and send events to the voice WS.

    This runs as an asyncio.Task so barge_in can be processed immediately.
    Delegates to _voice_turn_runner_streaming for entities with a streaming
    TTS pool (Cartesia).
    """
    # Check for streaming TTS pool (Cartesia)
    streaming_pool = None
    if _tts_manager:
        streaming_pool = _tts_manager.get_cartesia_pool(entity_cfg.name)

    if streaming_pool:
        await _voice_turn_runner_streaming(
            ws,
            sdk,
            entity_cfg,
            turn_id,
            transcript,
            cancel_event,
            streaming_pool,
            speaker=speaker,
        )
        return

    # No streaming TTS pool for this entity (Cartesia not connected).
    # The Deepgram batch fallback was removed in phase 5; fail loudly so the
    # client returns to idle instead of waiting on audio that never arrives.
    logger.error(
        "[voice] No streaming TTS pool for entity %s - aborting turn",
        entity_cfg.name,
    )
    try:
        await ws.send_json(
            {"type": "error", "message": "Kein TTS-Pool verbunden", "turn_id": turn_id}
        )
    except Exception:
        pass
    try:
        await ws.send_json(
            {
                "type": "turn_end",
                "turn_id": turn_id,
                "interrupted": True,
                "end_session": entity_cfg.single_turn,
            }
        )
    except Exception:
        pass


async def _voice_turn_runner_streaming(
    ws: WebSocket,
    sdk: SDKWrapper,
    entity_cfg: EntityConfig,
    turn_id: str,
    transcript: str,
    cancel_event: asyncio.Event,
    streaming_pool: list[Any],
    speaker: str = "",
    t_stt_done: float | None = None,
) -> None:
    """Background task: Producer-Consumer turn runner for streaming TTS pools.

    Works with CartesiaTTS session pools that expose the
    ``synthesize(text) -> AsyncIterator[bytes]`` interface.

    Three concurrent coroutines:
      1. _tts_worker — synthesize text on a pool session, push chunks to queue
      2. _event_producer — read Claude SDK events, dispatch TTS tasks
      3. _audio_consumer — stream audio to voice WS client in seq order
    """
    interrupted = False
    pool_idx = 0  # round-robin index into streaming_pool

    # Per-seq audio chunk queues: seq -> Queue[bytes | None]
    # None sentinel means "this seq is done"
    audio_queues: dict[int, asyncio.Queue[bytes | None]] = {}

    # Seq counter for ordering
    next_seq = 0

    # Track pending TTS tasks so we can cancel on barge-in
    tts_tasks: list[asyncio.Task[None]] = []

    # Event: producer is done (turn_end received or error)
    producer_done = asyncio.Event()

    # Total sentence count produced
    sentence_count = 0

    # Total bytes sent to voice WS (tracked by audio_consumer, read after gather)
    total_bytes_sent = 0

    # First-audio latency marker (duplex telemetry: stt_done → first frame)
    first_audio_sent = False

    # ── TTS Worker ───────────────────────────────────────────
    async def _tts_worker(
        session: Any,
        text: str,
        seq: int,
        context_id: str | None = None,
        continue_context: bool = False,
    ) -> None:
        """Synthesize text on a pool session, push chunks to its queue."""
        q = audio_queues[seq]

        for attempt in range(2):
            try:
                gen = session.synthesize(text)
                async for chunk in gen:
                    if cancel_event.is_set():
                        break
                    q.put_nowait(chunk)
                break  # success — exit retry loop
            except (ConnectionError, OSError) as exc:
                if attempt == 0:
                    logger.warning(
                        "[voice] TTS worker seq=%d transport error, retrying: %s",
                        seq,
                        exc,
                    )
                    try:
                        await session.reconnect()
                    except Exception:
                        logger.debug(
                            "[voice] TTS session reconnect failed", exc_info=True
                        )
                    continue  # retry once
                logger.error(
                    "[voice] TTS worker seq=%d failed after retry: %s", seq, exc
                )
            except Exception as exc:
                logger.error("[voice] TTS worker seq=%d error: %s", seq, exc)
                try:
                    await session.reconnect()
                except Exception:
                    logger.debug("[voice] TTS session reconnect failed", exc_info=True)
                break  # don't retry non-transport errors

        q.put_nowait(None)  # sentinel: done

    # ── Event Producer ───────────────────────────────────────
    async def _event_producer() -> None:
        nonlocal interrupted, next_seq, sentence_count, pool_idx

        try:
            # Speaker label (voice-ID / client-provided) rides in front of the
            # transcript so the model knows who is talking (empty in this
            # edition — no speaker identification). Logs keep the raw text.
            turn_text = (
                f"[Sprecher: {speaker}] {transcript}" if speaker else transcript
            )
            async for event in sdk.turn(turn_text, turn_id):
                if cancel_event.is_set():
                    interrupted = True
                    break

                etype = event.get("type")

                if etype == "sentence":
                    raw_text = event["text"]
                    clean_text, behaviors = _extract_behavior_tags(raw_text)
                    seq = next_seq
                    next_seq += 1
                    sentence_count += 1

                    # Send sentence JSON to client (text is clean, behaviors separate)
                    sentence_msg: dict[str, Any] = {
                        "type": "sentence",
                        "turn_id": turn_id,
                        "text": clean_text,
                        "voice_mode": "realtime",
                        "seq": seq,
                    }
                    if behaviors:
                        sentence_msg["behaviors"] = behaviors

                    try:
                        await ws.send_json(sentence_msg)
                    except Exception:
                        interrupted = True
                        break

                    # Dispatch TTS to pool (round-robin)
                    if clean_text.strip():
                        audio_queues[seq] = asyncio.Queue()
                        session_idx = pool_idx % len(streaming_pool)
                        session = streaming_pool[session_idx]
                        pool_idx += 1
                        logger.info(
                            "[voice] TTS dispatch: seq=%d → pool[%d], text='%s'",
                            seq,
                            session_idx,
                            clean_text[:50],
                        )

                        task = asyncio.create_task(
                            _tts_worker(session, clean_text, seq)
                        )
                        tts_tasks.append(task)
                    else:
                        # Empty text after tag stripping — no audio
                        audio_queues[seq] = asyncio.Queue()
                        audio_queues[seq].put_nowait(None)

                elif etype == "turn_end":
                    interrupted = event.get("interrupted", False)
                    break

                elif etype == "error":
                    try:
                        await ws.send_json(
                            {
                                "type": "error",
                                "message": event.get("message", "Unbekannter Fehler"),
                                "turn_id": turn_id,
                            }
                        )
                    except Exception:
                        pass
                    break

        except asyncio.CancelledError:
            interrupted = True
        except Exception as exc:
            logger.error("[voice] Event producer error: %s", exc, exc_info=True)
            try:
                await ws.send_json(
                    {
                        "type": "error",
                        "message": f"Turn-Fehler: {exc}",
                        "turn_id": turn_id,
                    }
                )
            except Exception:
                pass
        finally:
            producer_done.set()

    # ── Audio Consumer ───────────────────────────────────────
    async def _audio_consumer() -> None:
        """Stream audio chunks to the voice WS client in seq order."""
        nonlocal total_bytes_sent, first_audio_sent
        consumer_seq = 0

        while True:
            # Wait for the next seq's queue to appear, or producer done
            while consumer_seq not in audio_queues:
                if producer_done.is_set() and consumer_seq not in audio_queues:
                    return  # no more sentences coming
                if cancel_event.is_set():
                    return
                await asyncio.sleep(0.02)

            q = audio_queues[consumer_seq]

            # Send audio meta with streaming flag
            try:
                await ws.send_json(
                    {
                        "type": "audio",
                        "turn_id": turn_id,
                        "seq": consumer_seq,
                        "sample_rate": 24000,
                        "channels": 1,
                        "bits_per_sample": 16,
                        "streaming": True,
                    }
                )
            except Exception:
                return

            # Stream binary chunks until sentinel
            seq_bytes = 0
            seq_chunks = 0
            while True:
                if cancel_event.is_set():
                    return
                try:
                    chunk = await asyncio.wait_for(q.get(), timeout=5.0)
                except asyncio.TimeoutError:
                    # Don't break here — the TTS worker always sends a None
                    # sentinel via its finally block.  The queue can stay
                    # empty between sentences while the next one synthesizes.
                    logger.debug(
                        "[voice] audio_consumer: timeout waiting for seq=%d chunk (chunks=%d, bytes=%d)",
                        consumer_seq,
                        seq_chunks,
                        seq_bytes,
                    )
                    if cancel_event.is_set():
                        return
                    continue

                if chunk is None:
                    break  # sentinel: this seq is done

                seq_bytes += len(chunk)
                seq_chunks += 1
                try:
                    await ws.send_bytes(chunk)
                except Exception:
                    return
                if not first_audio_sent:
                    first_audio_sent = True
                    if t_stt_done is not None:
                        # THE number for perceived latency: everything between
                        # the finalized transcript and audible speech (identify
                        # + LLM TTFT + TTS TTFB + dispatch overhead).
                        logger.info(
                            "[voice] turn-latency: e2e=%dms (stt_done→first_audio) turn=%s",
                            int(
                                (asyncio.get_event_loop().time() - t_stt_done)
                                * 1000
                            ),
                            turn_id,
                        )

            total_bytes_sent += seq_bytes
            logger.info(
                "[voice] audio_consumer: seq=%d sent %d bytes (%d chunks, %.1fs audio)",
                consumer_seq,
                seq_bytes,
                seq_chunks,
                seq_bytes / 48000,
            )

            # Send audio_end for this seq
            try:
                await ws.send_json(
                    {
                        "type": "audio_end",
                        "turn_id": turn_id,
                        "seq": consumer_seq,
                    }
                )
            except Exception:
                return

            consumer_seq += 1

    # ── Run producer + consumer concurrently ─────────────────
    try:
        await asyncio.gather(
            _event_producer(),
            _audio_consumer(),
        )
    except asyncio.CancelledError:
        interrupted = True
    except Exception as exc:
        logger.error("[voice] streaming turn runner error: %s", exc, exc_info=True)

    # Cancel any remaining TTS tasks
    for task in tts_tasks:
        if not task.done():
            task.cancel()
    if tts_tasks:
        await asyncio.gather(*tts_tasks, return_exceptions=True)

    # Send turn_end FIRST — don't block on context usage check (~50-200ms pipe I/O)
    try:
        await ws.send_json(
            {
                "type": "turn_end",
                "turn_id": turn_id,
                "interrupted": interrupted,
                "end_session": entity_cfg.single_turn,
            }
        )
    except Exception:
        pass

    logger.info(
        "[voice] Turn fertig: %s (%d Sätze, %d bytes total, interrupted=%s, end_session=%s, pool=streaming)",
        turn_id,
        sentence_count,
        total_bytes_sent,
        interrupted,
        entity_cfg.single_turn,
    )

    # Context management (95% seam) runs explicitly in the duplex conversation
    # loop after playback (sdk.context_pct() + summarize_and_reconnect()), NOT
    # here — this streaming path must not block turn_end delivery on a
    # get_context_usage() pipe round-trip.

    # For callers that care (duplex retry-on-empty + drain-timeout sizing);
    # /voice/stream ignores this.
    return sentence_count, interrupted, total_bytes_sent


@app.websocket("/voice/stream")
async def voice_websocket(ws: WebSocket):
    """Voice WebSocket: one turn from a ready transcript (test clients).

    Singleton — nur eine Voice-Verbindung gleichzeitig.

    Client → Server::

        {"type": "turn_start", "turn_id": "...", "transcript": "...",
         "entity": "robbie", "speaker": "", "channel": "voice"}
        {"type": "barge_in", "turn_id": "..."}
        {"type": "entity_wake", "entity": "robbie"}
        {"type": "entity_sleep", "entity": "robbie"}
        {"type": "ping"}

    Server → Client::

        {"type": "turn_ack", "turn_id": "...", "entity": "..."}
        {"type": "sentence", "turn_id": "...", "text": "...",
         "voice_mode": "realtime", "seq": N, "behaviors": ["excited"]}
        {"type": "audio", "turn_id": "...", "seq": N, "sample_rate": 24000,
         "channels": 1, "bits_per_sample": 16, "streaming": true}
        (binary frame: PCM16 audio chunk)
        {"type": "audio_end", "turn_id": "...", "seq": N}
        {"type": "turn_end", "turn_id": "...", "interrupted": false,
         "end_session": false}
        {"type": "entity_wake_ack", "entity": "...", "ducked": true}
        {"type": "pong"}
        {"type": "error", "message": "..."}
    """
    global _voice_ws, _voice_turn_task, _voice_cancel_event

    # Singleton check
    if _voice_ws is not None:
        await ws.accept()
        await ws.send_json(
            {"type": "error", "message": "Bereits ein Voice-Client verbunden"}
        )
        await ws.close(code=1008, reason="Already connected")
        return

    await ws.accept()
    _voice_ws = ws
    _voice_cancel_event = asyncio.Event()

    logger.info("Voice WS verbunden (%s)", ws.client)

    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await ws.send_json({"type": "error", "message": "Ungültiges JSON"})
                continue

            msg_type = data.get("type", "")

            # ── turn_start ───────────────────────────────────
            if msg_type == "turn_start":
                turn_id = data.get("turn_id") or uuid.uuid4().hex[:12]
                transcript = data.get("transcript", "").strip()
                entity_name = data.get("entity")
                speaker = (data.get("speaker") or "").strip().lower()

                if not transcript:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Leeres Transkript",
                            "turn_id": turn_id,
                        }
                    )
                    continue

                if not entity_manager:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Server startet noch",
                            "turn_id": turn_id,
                        }
                    )
                    continue

                try:
                    sdk, entity_cfg = entity_manager.get(entity_name)
                except KeyError as exc:
                    await ws.send_json(
                        {"type": "error", "message": str(exc), "turn_id": turn_id}
                    )
                    continue

                if not sdk.connected:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": f"Entity '{entity_cfg.name}' nicht verbunden",
                            "turn_id": turn_id,
                        }
                    )
                    continue

                # Check if a turn is already running
                if _voice_turn_task is not None and not _voice_turn_task.done():
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "Ein Turn läuft bereits",
                            "turn_id": turn_id,
                        }
                    )
                    continue

                if sdk.turn_active:
                    await ws.send_json(
                        {
                            "type": "error",
                            "message": "SDK Turn-Lock belegt (Chat?)",
                            "turn_id": turn_id,
                        }
                    )
                    continue

                # Send ACK
                await ws.send_json(
                    {
                        "type": "turn_ack",
                        "turn_id": turn_id,
                        "entity": entity_cfg.name,
                    }
                )

                logger.info(
                    "[voice] turn_start: entity=%s, turn=%s, speaker=%s, transcript=%s",
                    entity_cfg.name,
                    turn_id,
                    data.get("speaker", "?"),
                    transcript[:60],
                )

                # Start background turn task (normal SDK path)
                _voice_cancel_event.clear()
                _voice_turn_task = asyncio.create_task(
                    _voice_turn_runner(
                        ws,
                        sdk,
                        entity_cfg,
                        turn_id,
                        transcript,
                        _voice_cancel_event,
                        speaker=speaker,
                    )
                )

            # ── barge_in ─────────────────────────────────────
            elif msg_type == "barge_in":
                turn_id = data.get("turn_id", "?")
                logger.info("[voice] Barge-In: turn=%s", turn_id)

                _voice_cancel_event.set()

                entity_name = data.get("entity")
                if entity_manager:
                    try:
                        sdk, _ = entity_manager.get(entity_name)
                        await sdk.interrupt()
                    except KeyError:
                        # Try to interrupt default
                        try:
                            sdk, _ = entity_manager.get()
                            await sdk.interrupt()
                        except KeyError:
                            pass

            # ── entity_wake / entity_sleep ───────────────────
            # Kept for protocol compatibility. In the family setup these
            # duck/unduck a music player; this edition has no player, so
            # wake is acknowledged with ducked=false and sleep is a no-op.
            elif msg_type == "entity_wake":
                ent = data.get("entity", "")
                logger.info("[voice] entity_wake: %s", ent)
                await ws.send_json(
                    {"type": "entity_wake_ack", "entity": ent, "ducked": False}
                )

            elif msg_type == "entity_sleep":
                logger.info("[voice] entity_sleep: %s", data.get("entity", ""))

            # ── cancel ───────────────────────────────────────
            elif msg_type == "cancel":
                turn_id = data.get("turn_id", "?")
                logger.info("[voice] Cancel: turn=%s", turn_id)
                _voice_cancel_event.set()

            # ── ping / pong ──────────────────────────────────
            elif msg_type == "ping":
                await ws.send_json({"type": "pong"})

            # ── unknown ──────────────────────────────────────
            else:
                logger.debug("[voice] Unbekannter Typ: %s", msg_type)

    except WebSocketDisconnect:
        logger.info("Voice WS getrennt (normal)")
    except Exception as exc:
        logger.error("Voice WS Fehler: %s", exc, exc_info=True)
    finally:
        # Cancel any running turn
        if _voice_turn_task is not None and not _voice_turn_task.done():
            _voice_cancel_event.set()
            _voice_turn_task.cancel()
            try:
                await _voice_turn_task
            except (asyncio.CancelledError, Exception):
                pass
            _voice_turn_task = None

        _voice_ws = None
        logger.info("Voice WS Cleanup abgeschlossen")


# =====================================================================
#  Duplex Voice WebSocket  (/voice/duplex)  — the station's persistent uplink
# =====================================================================


@app.websocket("/voice/duplex")
async def voice_duplex(ws: WebSocket):
    """Persistent Pi-station uplink (ring buffer + server-side wakeword).

    Logic lives in server/duplex.py. Imported lazily so a missing voice
    dependency (openwakeword/numpy) degrades this endpoint only, never the
    rest of the server.
    """
    try:
        from server import duplex as duplex_mod
    except Exception as exc:
        logger.error("[duplex] module import failed: %s", exc)
        await ws.accept()
        await ws.send_json(
            {"type": "error", "message": f"Duplex nicht verfügbar: {exc}"}
        )
        await ws.close(code=1011)
        return

    # Pass THIS live module (not a fresh `import server.app`): under
    # `python -m server.app` the running module is __main__, and a re-import
    # would create a second instance whose lifespan globals were never set.
    await duplex_mod.handle_duplex(ws, sys.modules[__name__])


@app.post("/api/notify")
async def api_notify(request: Request):
    """Push a spoken notification to the duplex station.

    Body: {"text": "..."} — queued like a timer notification and spoken at
    the station as soon as it is idle. Doubles as the E2E test hook for the
    capability delivery chain (and as a "say something" API for scripts).
    """
    from server.capabilities import Notification

    if capability_manager is None:
        return JSONResponse({"error": "capabilities not running"}, status_code=503)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)

    await capability_manager.notification_queue.put(
        Notification(text=text, source="api")
    )
    try:
        from server import duplex as duplex_mod

        station_connected = duplex_mod._duplex_ws is not None
    except Exception:
        station_connected = False
    return {"queued": True, "station_connected": station_connected}


@app.post("/api/capabilities/command")
async def api_capability_command(request: Request):
    """Run a transcript through the capability regex fast-path (headless test).

    Body: {"text": "..."} — same entry point a duplex turn uses before the
    LLM. Side effects are real (a matched switch command actually switches);
    completion notifications are spoken at the station once it is idle.
    """
    if capability_manager is None:
        return JSONResponse({"error": "capabilities not running"}, status_code=503)
    body = await request.json()
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)

    matched = capability_manager.match_command(text)
    if matched is None:
        return {"matched": None}
    cap_name, result = matched
    return {
        "matched": cap_name,
        "text": result.text,
        "end_conversation": result.end_conversation,
    }


# =====================================================================
#  Entry Point
# =====================================================================


def _find_entities_toml() -> str:
    """Locate the main config file.

    Split layout (preferred): server/config/config.toml (with plugins.toml +
    prompt.toml siblings). Legacy fallback: monolithic server/entities.toml.
    """
    here = Path(__file__).parent
    split = here / "config" / "config.toml"
    if split.exists():
        return str(split)
    legacy = here / "entities.toml"
    if legacy.exists():
        return str(legacy)
    # Fallback: CWD
    cwd = Path.cwd() / "entities.toml"
    if cwd.exists():
        return str(cwd)
    # Default path
    return str(split)


def _find_config_toml() -> str:
    """Locate the infra config (host/port/mcp_vars). In the split layout
    this is the same file as --entities (server/config/config.toml)."""
    here = Path(__file__).resolve().parent
    split = here / "config" / "config.toml"
    if split.exists():
        return str(split)
    return str(here / "config.toml")


def _load_config(path: str) -> dict:
    """Load config.toml and return as nested dict."""
    p = Path(path)
    if not p.exists():
        logger.warning("config.toml not found at %s — using defaults", path)
        return {}
    with open(p, "rb") as f:
        return tomllib.load(f)


def parse_args() -> argparse.Namespace:
    """CLI-Argumente parsen."""
    parser = argparse.ArgumentParser(
        description="Robbie server — voice assistant brain",
    )
    parser.add_argument(
        "--config",
        default=_find_config_toml(),
        help="Pfad zur Infra-Config (default: server/config/config.toml)",
    )
    parser.add_argument(
        "--entities",
        default=_find_entities_toml(),
        help=(
            "Pfad zur Haupt-Config; plugins.toml/prompt.toml daneben werden "
            "gemergt (default: server/config/config.toml)"
        ),
    )
    # CLI overrides (optional, defaults come from config.toml)
    parser.add_argument(
        "--host", default=None, help="Bind-Adresse (override config.toml)"
    )
    parser.add_argument(
        "--port", type=int, default=None, help="Server-Port (override config.toml)"
    )
    return parser.parse_args()


def main():
    """Server starten."""
    global _cli_args, _config
    _cli_args = parse_args()
    _config = _load_config(_cli_args.config)

    # CLI args override config.toml
    if _cli_args.host is None:
        _cli_args.host = _config.get("server", {}).get("host", "0.0.0.0")
    if _cli_args.port is None:
        _cli_args.port = _config.get("server", {}).get("port", 8422)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        logger.warning(
            "ANTHROPIC_API_KEY is not set — the LLM will not answer. "
            "Put it into .env (see .env.example)."
        )

    logger.info("═" * 60)
    logger.info("  Robbie Server")
    logger.info("  Config:   %s", _cli_args.config)
    logger.info("  Entities: %s", _cli_args.entities)
    logger.info("  Port:     %d", _cli_args.port)
    logger.info("═" * 60)

    uvicorn.run(
        app,
        host=_cli_args.host,
        port=_cli_args.port,
        log_level="warning",
        ws_ping_interval=30,
        ws_ping_timeout=30,
    )


if __name__ == "__main__":
    main()
