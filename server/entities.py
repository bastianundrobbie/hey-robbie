"""Entity configuration, SDK wrappers, and lifecycle management.

Extracted from ``app.py`` — contains all entity-related code:

- ``MCPServerDef`` / ``EntityConfig`` dataclasses
- ``load_entities()`` — TOML parser for the split config (config.toml +
  plugins.toml + prompt.toml)
- ``SDKWrapper`` — persistent ``ClaudeSDKClient`` per entity
- ``EntityManager`` — lifecycle for all entities

Authentication: the Claude Agent SDK subprocess reads ``ANTHROPIC_API_KEY``
from the environment (set it in ``.env``). Nothing else is needed.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

import tomllib

from server.sentence_splitter import _MIN_SENTENCE_LEN
from server.sentence_splitter import extract_text_delta as _extract_text_delta
from server.sentence_splitter import split_sentences as _split_sentences

logger = logging.getLogger("robbie-server")


# =====================================================================
#  Entity Config
# =====================================================================


@dataclass
class MCPServerDef:
    """MCP Server Definition aus [mcp_servers.X] in entities.toml."""

    name: str
    type: str = "stdio"
    command: str = ""  # leer = sys.executable (Python)
    module: str = ""  # wenn gesetzt: args werden zu ["-m", module] + args
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    description: str = ""
    prompt: str = (
        ""  # Prompt-Injection: wird automatisch in den Entity-System-Prompt eingefügt
    )


@dataclass
class EntityConfig:
    """Konfiguration einer einzelnen Entity (aus entities.toml)."""

    name: str
    display_name: str = ""
    model: str = "sonnet"
    system_prompt: str = ""
    max_turns: int = 10
    single_turn: bool = False
    permission_mode: str = "bypassPermissions"
    # SDK tuning
    effort: str = ""  # "low", "medium", "high", "max" oder "" (SDK-Default)
    bare: bool = False  # --bare: skip hooks, LSP, auto-memory, CLAUDE.md discovery
    tools: str = ""  # --tools: built-in tool whitelist ("" = alle, "none" = keine)

    # Voice/TTS (for GET /entities)
    wakeword: str = ""
    tts_provider: str = ""
    tts_model: str = ""
    tts_voice_id: str = ""
    tts_speed: float = 1.0  # Cartesia Sonic 3: 0.6–1.5x (default 1.0)
    tts_gain: float = 0.0
    tts_style: str = ""

    # MCP servers (referenziert [mcp_servers.X] aus plugins.toml)
    mcp: list[str] = field(default_factory=list)

    def voice_dict(self) -> dict[str, Any]:
        """Entity-Info für GET /entities (Voice-Client + Web Dashboard).

        Format: ``id`` statt ``name``, plus ``status`` Feld.
        """
        return {
            "id": self.name,
            "display_name": self.display_name,
            "status": "active",
            "wakeword": self.wakeword,
            "tts_provider": self.tts_provider,
            "tts_model": self.tts_model,
            "tts_voice_id": self.tts_voice_id,
            "tts_gain": self.tts_gain,
            "model": self.model,
            "effort": self.effort,
            "mcp": list(self.mcp),
            "max_turns": self.max_turns,
        }


def load_entities(
    path: str | Path,
) -> tuple[str, list[EntityConfig], dict[str, MCPServerDef]]:
    """Lade Entity-Konfigurationen und MCP-Server-Definitionen aus der Config.

    Returns:
        (default_entity_name, list_of_entity_configs, mcp_server_registry)
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Config nicht gefunden: {p}")

    with open(p, "rb") as f:
        data = tomllib.load(f)

    # ── Split layout (since 2026-07-10): plugins.toml + prompt.toml siblings ──
    # plugins.toml: [mcp_servers.*] definitions + top-level `enabled` list
    #               (= the default entity's mcp assignment, order matters).
    # prompt.toml:  top-level `system_prompt` for the default entity.
    # A monolithic entities.toml without siblings keeps working unchanged.
    plugins_path = p.with_name("plugins.toml")
    enabled: list | None = None
    if plugins_path.exists():
        with open(plugins_path, "rb") as f:
            plugins_data = tomllib.load(f)
        data.setdefault("mcp_servers", {}).update(
            plugins_data.get("mcp_servers", {})
        )
        if "enabled" in plugins_data:
            enabled = list(plugins_data["enabled"])
        logger.info("Split-Config: plugins.toml geladen (%s)", plugins_path)

    prompt_path = p.with_name("prompt.toml")
    prompt_text: str | None = None
    if prompt_path.exists():
        with open(prompt_path, "rb") as f:
            prompt_text = tomllib.load(f).get("system_prompt", "")
        logger.info("Split-Config: prompt.toml geladen (%s)", prompt_path)

    if enabled is not None or prompt_text is not None:
        entities_sec = data.get("entities", {})
        target = data.get("server", {}).get("default_entity") or next(
            iter(entities_sec), ""
        )
        if target and target in entities_sec:
            if enabled is not None:
                entities_sec[target]["mcp"] = enabled
            if prompt_text is not None:
                entities_sec[target]["system_prompt"] = prompt_text
        else:
            raise ValueError(
                "Split-Config: Default-Entity für plugins/prompt.toml nicht gefunden"
            )

    default_entity = data.get("server", {}).get("default_entity", "")

    # ── MCP Server Definitions ──
    mcp_registry: dict[str, MCPServerDef] = {}
    for name, values in data.get("mcp_servers", {}).items():
        mcp_def = MCPServerDef(
            name=name,
            type=values.get("type", "stdio"),
            command=values.get("command", ""),
            module=values.get("module", ""),
            args=values.get("args", []),
            env=values.get("env", {}),
            description=values.get("description", ""),
            prompt=values.get("prompt", ""),
        )
        mcp_registry[name] = mcp_def
        logger.info("MCP Server definiert: %s (module=%s)", name, mcp_def.module)

    # ── Entities ──
    entities_section = data.get("entities", {})

    configs: list[EntityConfig] = []
    for name, values in entities_section.items():
        mcp_list: list[str] = list(values.get("mcp", []))

        # Validate MCP references
        for mcp_name in mcp_list:
            if mcp_name not in mcp_registry:
                raise ValueError(
                    f"Entity '{name}' referenziert unbekannten MCP Server '{mcp_name}'. "
                    f"Definiert: {list(mcp_registry.keys())}"
                )

        cfg = EntityConfig(
            name=name,
            display_name=values.get("display_name", name),
            model=values.get("model", "sonnet"),
            system_prompt=values.get("system_prompt", ""),
            max_turns=values.get("max_turns", 10),
            single_turn=values.get("single_turn", False),
            permission_mode=values.get("permission_mode", "bypassPermissions"),
            effort=values.get("effort", ""),
            bare=values.get("bare", False),
            tools=values.get("tools", ""),
            wakeword=values.get("wakeword", ""),
            tts_provider=values.get("tts_provider", ""),
            tts_model=values.get("tts_model", ""),
            tts_voice_id=values.get("tts_voice_id", ""),
            tts_speed=values.get("tts_speed", 1.0),
            tts_gain=values.get("tts_gain", 0.0),
            tts_style=values.get("tts_style", ""),
            mcp=mcp_list,
        )
        configs.append(cfg)
        logger.info(
            "Entity geladen: %s (model=%s, mcp=%s, effort=%s, bare=%s, tools=%s)",
            name,
            cfg.model,
            cfg.mcp or "none",
            cfg.effort or "default",
            cfg.bare,
            cfg.tools or "default",
        )

    if not configs:
        raise ValueError("Keine Entities in config.toml definiert")

    if not default_entity:
        default_entity = configs[0].name

    return default_entity, configs, mcp_registry


# =====================================================================
#  Session continuity (in-memory)
# =====================================================================
#
# At the ~95% context seam Robbie summarizes its OWN session (it is the only
# thing that can read the otherwise-opaque SDK context) and the SDK session is
# recycled. The summary is re-fed on the first turn after the reconnect, so a
# conversation survives the reset. The summary lives in RAM only — nothing is
# written to disk in this edition.

# Self-summary prompt for the seam. Tail-aware: if the conversation is still
# live, Robbie appends the last exchanges verbatim so it can continue without
# replaying the whole (expensive) context. "NICHTS" => nothing worth keeping.
_SUMMARY_PROMPT = (
    "Fasse dieses Gespräch für dein eigenes Gedächtnis in wenigen Sätzen zusammen — "
    "nur inhaltlich Wichtiges: Themen, Vereinbarungen, offene Fäden, Stimmung. "
    "Lass Belanglosigkeiten weg (z. B. Uhrzeit-Fragen, Timer). "
    "Falls das Gespräch gerade noch aktiv läuft, hänge die letzten zwei bis drei "
    "Wortwechsel wörtlich an, damit du nahtlos weiterreden kannst. "
    "Antworte NUR mit der Zusammenfassung, ohne Vorrede. "
    "Wenn es nichts Merkenswertes gab, antworte ausschließlich mit dem Wort NICHTS."
)


# =====================================================================
#  SDK Wrapper — Persistenter ClaudeSDKClient (pro Entity)
# =====================================================================


class SDKWrapper:
    """Kapselt einen persistenten ClaudeSDKClient für eine Entity.

    Lifecycle:
      1. ``connect()``  — startet claude.exe, öffnet Pipes
      2. ``turn(text)``  — async generator, yieldet Sentences
      3. ``interrupt()`` — Barge-In
      4. ``disconnect()`` — beendet claude.exe
    """

    def __init__(
        self,
        entity_name: str,
        options: Any,  # ClaudeAgentOptions — imported lazily
    ) -> None:
        self._entity_name = entity_name
        self._options = options
        self._client: Any = None  # ClaudeSDKClient
        self._connected = False
        self._turn_lock = asyncio.Lock()
        self._active_turn_id: str | None = None
        self._interrupted = False
        # Episode text queued after a context-seam reconnect, prepended to the
        # next turn so Robbie continues where it left off. Cleared on use.
        self._pending_injection: str | None = None
        # True from connect() until the first turn of the session succeeds — the
        # cue to inject Robbie's notepad board (read live) once per session.
        self._fresh_session = False
        # Monotonic timestamp of the previous turn's end — the idle gap in the
        # turn telemetry (latenz.md). None until the first turn completes.
        self._last_turn_end_mono: float | None = None

    @property
    def entity_name(self) -> str:
        return self._entity_name

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def model(self) -> str:
        return getattr(self._options, "model", "unknown")

    def update_options(self, options: Any) -> None:
        """Replace the SDK options used on the next connect().

        Does NOT reconnect — the caller must disconnect()/connect() for the
        change (e.g. model/effort) to take effect on the running process.
        """
        self._options = options

    async def connect(self) -> None:
        """Starte den persistenten Claude-Prozess."""
        if self._connected:
            return

        from claude_agent_sdk import ClaudeSDKClient  # type: ignore[import-not-found]

        self._client = ClaudeSDKClient(options=self._options)

        logger.info(
            "[%s] Starte Claude-Prozess (model=%s) …",
            self._entity_name,
            self.model,
        )
        t0 = time.monotonic()
        await self._client.connect()
        elapsed = time.monotonic() - t0
        self._connected = True
        # Fresh SDK session — notepad board is due on the first turn (any
        # connect: server start, failover, model change, 95% reconnect).
        self._fresh_session = True
        logger.info("[%s] Claude-Prozess bereit (%.1fs)", self._entity_name, elapsed)

    async def disconnect(self) -> None:
        """Beende den Claude-Prozess."""
        if self._client is not None:
            try:
                await self._client.disconnect()
            except Exception:
                logger.debug(
                    "[%s] disconnect() Fehler", self._entity_name, exc_info=True
                )
            self._client = None
        self._connected = False
        logger.info("[%s] Claude-Prozess beendet", self._entity_name)

    async def interrupt(self) -> None:
        """Sende Interrupt-Signal (Barge-In)."""
        self._interrupted = True
        if self._client is not None:
            try:
                await self._client.interrupt()
                logger.info(
                    "[%s] Interrupt gesendet (turn=%s)",
                    self._entity_name,
                    self._active_turn_id,
                )
            except Exception:
                logger.debug(
                    "[%s] interrupt() Fehler", self._entity_name, exc_info=True
                )

    async def get_context_usage(self) -> dict[str, Any] | None:
        """Query the SDK for current context window usage.

        Returns a dict like::

            {
                "totalTokens": 9248,
                "maxTokens": 200000,
                "percentage": 5,
                "model": "claude-sonnet-4-6",
                "categories": [...]
            }

        Returns ``None`` if not connected or on error.
        """
        if not self._connected or self._client is None:
            return None
        try:
            usage = await self._client.get_context_usage()
            return usage if isinstance(usage, dict) else None
        except Exception as exc:
            logger.debug("[%s] get_context_usage() error: %s", self._entity_name, exc)
            return None

    async def context_pct(self) -> int:
        """Current context-window fill in percent (0 if unknown)."""
        usage = await self.get_context_usage()
        if not usage:
            return 0
        try:
            return int(usage.get("percentage", 0) or 0)
        except (TypeError, ValueError):
            return 0

    async def _run_silent_query(self, prompt: str) -> str:
        """Run one query and collect the full text WITHOUT streaming to TTS.

        Used for the off-path self-summary at the context seam. The caller must
        hold the turn lock and the client must be connected.
        """
        if not self._connected or self._client is None:
            return ""
        try:
            await self._client.query(prompt)
        except Exception as exc:
            logger.warning("[%s] silent query() failed: %s", self._entity_name, exc)
            return ""
        buf = ""
        try:
            async for msg in self._client.receive_response():
                mt = type(msg).__name__
                if mt == "StreamEvent":
                    delta = _extract_text_delta(msg)
                    if delta:
                        buf += delta
                elif mt == "ResultMessage":
                    break
        except Exception as exc:
            logger.warning("[%s] silent receive failed: %s", self._entity_name, exc)
        return buf.strip()

    async def summarize_and_reconnect(
        self, summary_timeout: float | None = None
    ) -> None:
        """Context-seam handler (~95%): Robbie summarizes its own session into
        an episode, then the SDK session is recycled. The summary is re-fed on
        the next turn via ``_pending_injection``. Off the hot path — the caller
        invokes this after the answer has already been spoken.

        ``summary_timeout`` bounds the silent summary query (voice-controlled
        model switch: a hung old model must not brick the entity under the
        held turn lock); ``None`` keeps the unbounded seam behaviour.
        """
        async with self._turn_lock:
            if summary_timeout is None:
                summary = await self._run_silent_query(_SUMMARY_PROMPT)
            else:
                try:
                    summary = await asyncio.wait_for(
                        self._run_silent_query(_SUMMARY_PROMPT), summary_timeout
                    )
                except asyncio.TimeoutError:
                    logger.warning(
                        "[%s] summary query timed out after %.0fs — "
                        "reconnecting without episode",
                        self._entity_name,
                        summary_timeout,
                    )
                    summary = ""
            keep = summary and summary.rstrip(" .!").upper() != "NICHTS"
            if keep:
                logger.info(
                    "[%s] session summary kept (%d chars)",
                    self._entity_name,
                    len(summary),
                )
            else:
                logger.info(
                    "[%s] no session summary (empty/NICHTS)", self._entity_name
                )

            await self.disconnect()
            await self.connect()
            self._pending_injection = summary if keep else None
            if self._pending_injection:
                logger.info(
                    "[%s] context reset — summary queued for injection",
                    self._entity_name,
                )

    @property
    def turn_active(self) -> bool:
        return self._turn_lock.locked()

    async def turn(
        self, text: str, turn_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Sende einen Turn und yielde Sentence-Dicts.

        Yields:
            {"type": "sentence", "turn_id": ..., "text": ..., "seq": N}
            {"type": "turn_end", "turn_id": ..., "interrupted": bool, ...}

        Raises:
            RuntimeError: Wenn nicht verbunden oder Turn-Lock belegt.
        """
        if not self._connected or self._client is None:
            raise RuntimeError("Nicht verbunden")

        if self._turn_lock.locked():
            raise RuntimeError("Ein anderer Turn läuft bereits")

        tid = turn_id or uuid.uuid4().hex[:12]

        async with self._turn_lock:
            self._active_turn_id = tid
            self._interrupted = False
            t0 = time.monotonic()
            # Telemetry (latenz.md Mess-Rezept): idle gap since the previous
            # turn + whether this is the first turn of a fresh SDK session
            # (reconnect / failover / model change) — captured here because
            # _fresh_session is cleared further down once a sentence went out.
            idle_s = (
                round(t0 - self._last_turn_end_mono, 1)
                if self._last_turn_end_mono is not None
                else None
            )
            was_fresh = self._fresh_session

            # Session-start injection: prepend the queued summary (after a
            # context-seam reconnect) so Robbie continues where it left off.
            # Sits in front of the later "[Sprecher: name]" prefix (app.py).
            # NOTE: only CLEAR the flags once this turn actually produced a
            # sentence (see seq check at turn_end). A fresh session's first query
            # often returns an empty answer (SDK quirk) and gets retried —
            # clearing here would spend the memory on the empty attempt and the
            # real answer would run without it.
            parts: list[str] = []
            if self._pending_injection:
                parts.append(
                    f"[Erinnerung aus früheren Gesprächen: {self._pending_injection}]"
                )
            injected = bool(parts)
            if injected:
                text = "\n\n".join(parts) + "\n\n" + text

            logger.info(
                "[%s] Turn gestartet: %s [%s]",
                self._entity_name,
                tid,
                text[:80] + ("…" if len(text) > 80 else ""),
            )

            try:
                await self._client.query(text)
            except Exception as exc:
                logger.error("[%s] query() fehlgeschlagen: %s", self._entity_name, exc)
                yield {
                    "type": "error",
                    "message": f"query() Fehler: {exc}",
                    "turn_id": tid,
                }
                return

            seq = 0
            text_buffer = ""
            interrupted = False
            t_first: float | None = None  # first sentence out → TTFT
            sdk_usage: dict[str, Any] = {}  # ResultMessage.usage (cache counters)
            sdk_duration: Any = None

            try:
                async for msg in self._client.receive_response():
                    if self._interrupted:
                        interrupted = True
                        break

                    msg_type = type(msg).__name__
                    logger.debug(
                        "[%s] SDK msg: %s — %s",
                        self._entity_name,
                        msg_type,
                        str(msg)[:200],
                    )

                    if msg_type == "AssistantMessage":
                        # Check for billing/API errors returned as AssistantMessage
                        err = getattr(msg, "error", None)
                        if err:
                            content = getattr(msg, "content", [])
                            err_text = ""
                            for block in content:
                                if hasattr(block, "text"):
                                    err_text = block.text
                                    break
                            logger.warning(
                                "[%s] SDK error: %s — %s (turn=%s)",
                                self._entity_name,
                                err,
                                err_text,
                                tid,
                            )
                            yield {
                                "type": "error",
                                "turn_id": tid,
                                "message": f"{err}: {err_text}",
                            }

                    elif msg_type == "StreamEvent":
                        new_text = _extract_text_delta(msg)
                        if new_text:
                            text_buffer += new_text
                            # First sentence: min_len=1 for fast acknowledgment dispatch
                            # ("Klar!", "Ja!", "Genau!") → immediate TTS, no buffering
                            min_len = 1 if seq == 0 else _MIN_SENTENCE_LEN
                            sentences, text_buffer = _split_sentences(
                                text_buffer, min_len=min_len
                            )
                            for sentence in sentences:
                                if t_first is None:
                                    t_first = time.monotonic()
                                yield {
                                    "type": "sentence",
                                    "turn_id": tid,
                                    "text": sentence,
                                    "seq": seq,
                                }
                                seq += 1

                    elif msg_type == "ResultMessage":
                        sdk_duration = getattr(msg, "duration_ms", None)
                        u = getattr(msg, "usage", None)
                        if isinstance(u, dict):
                            sdk_usage = u
                        logger.info(
                            "[%s] Turn abgeschlossen: %s (sdk_duration=%sms)",
                            self._entity_name,
                            tid,
                            sdk_duration or "?",
                        )
                        break

                    elif "Compact" in msg_type:
                        # SDK-side compaction boundary — log it so the telemetry
                        # can correlate context shrink with cache re-creation.
                        logger.info(
                            "[%s] compaction event: %s (turn=%s)",
                            self._entity_name,
                            msg_type,
                            tid,
                        )

                    elif msg_type == "RateLimitEvent":
                        logger.warning(
                            "[%s] Rate-Limit (turn=%s)", self._entity_name, tid
                        )

            except asyncio.CancelledError:
                interrupted = True
            except Exception as exc:
                logger.error(
                    "[%s] receive_response Fehler: %s",
                    self._entity_name,
                    exc,
                    exc_info=True,
                )
                yield {
                    "type": "error",
                    "message": f"Receive-Fehler: {exc}",
                    "turn_id": tid,
                }

            # Rest-Buffer flushen
            if not interrupted and text_buffer.strip():
                if t_first is None:
                    t_first = time.monotonic()
                yield {
                    "type": "sentence",
                    "turn_id": tid,
                    "text": text_buffer.strip(),
                    "seq": seq,
                }
                seq += 1

            # Only now spend the injected context — a real answer went out. An
            # empty attempt (seq == 0, e.g. the SDK's empty first answer after a
            # reconnect, or a failover error) leaves both queued for the retry.
            if injected and seq > 0:
                self._pending_injection = None
                self._fresh_session = False

            elapsed_ms = int((time.monotonic() - t0) * 1000)

            # Turn telemetry (latenz.md Mess-Rezept). cache_creation >> cache_read
            # after a long idle gap = cold prefix (H1); fresh=True marks the first
            # turn of a new SDK session (post reconnect/failover). Logged BEFORE
            # the turn_end yield — the duplex consumer breaks on turn_end, so the
            # generator tail below it never runs.
            ttft_ms = int((t_first - t0) * 1000) if t_first is not None else None
            logger.info(
                "[%s] turn-telemetry: %s idle=%s ttft=%s cache_read=%s "
                "cache_creation=%s input=%s fresh=%s sdk_duration=%sms "
                "sentences=%d interrupted=%s",
                self._entity_name,
                tid,
                f"{idle_s}s" if idle_s is not None else "-",
                f"{ttft_ms}ms" if ttft_ms is not None else "-",
                sdk_usage.get("cache_read_input_tokens", "-"),
                sdk_usage.get("cache_creation_input_tokens", "-"),
                sdk_usage.get("input_tokens", "-"),
                was_fresh,
                sdk_duration if sdk_duration is not None else "-",
                seq,
                interrupted,
            )
            self._last_turn_end_mono = time.monotonic()

            yield {
                "type": "turn_end",
                "turn_id": tid,
                "interrupted": interrupted,
                "duration_ms": elapsed_ms,
                "sentence_count": seq,
            }

            self._active_turn_id = None
            logger.info(
                "[%s] Turn beendet: %s (%dms, %d Sätze, interrupted=%s)",
                self._entity_name,
                tid,
                elapsed_ms,
                seq,
                interrupted,
            )
            # NOTE: the context safety-net used to live here (every 15 turns),
            # but this generator tail never runs in the duplex path — the
            # consumer breaks on turn_end. Context management now runs
            # explicitly in the duplex conversation loop via
            # context_pct() + summarize_and_reconnect().


# =====================================================================
#  Entity Manager — Lifecycle for all entities
# =====================================================================


class EntityManager:
    """Verwaltet alle Entity-SDKWrapper."""

    def __init__(
        self,
        default_entity: str,
        configs: list[EntityConfig],
        mcp_registry: dict[str, MCPServerDef],
        config: dict | None = None,
    ) -> None:
        self._default_entity = default_entity
        self._configs: dict[str, EntityConfig] = {c.name: c for c in configs}
        self._wrappers: dict[str, SDKWrapper] = {}
        self._failed: dict[str, str] = {}
        self._mcp_registry = mcp_registry
        # {var} placeholders usable in [mcp_servers.*].args — every key of the
        # optional [mcp_vars] table in config.toml (e.g. weather_lat).
        cfg = config or {}
        self._mcp_vars = {
            str(k): str(v) for k, v in (cfg.get("mcp_vars") or {}).items()
        }

        for cfg in configs:
            options = self._build_options(cfg)
            self._wrappers[cfg.name] = SDKWrapper(cfg.name, options)

    def _build_options(self, cfg: EntityConfig) -> Any:
        """Build ClaudeAgentOptions for an entity."""
        from claude_agent_sdk import (  # type: ignore[import-not-found]
            ClaudeAgentOptions,
        )

        mcp_servers: dict[str, Any] = {}

        for mcp_name in cfg.mcp:
            mcp_def = self._mcp_registry[mcp_name]
            # Resolve {var} placeholders in args
            resolved_args = [a.format(**self._mcp_vars) for a in mcp_def.args]
            # Build command + args
            command = mcp_def.command or sys.executable
            if mcp_def.module:
                full_args = ["-m", mcp_def.module] + resolved_args
            else:
                full_args = resolved_args
            mcp_servers[mcp_name] = {
                "type": mcp_def.type,
                "command": command,
                "args": full_args,
                "env": dict(mcp_def.env),
            }

        options = ClaudeAgentOptions(
            model=cfg.model,
            permission_mode=cfg.permission_mode,  # pyright: ignore[reportArgumentType]
            include_partial_messages=True,
            max_turns=cfg.max_turns,
        )
        if cfg.system_prompt:
            # Auto-inject MCP tool descriptions into system prompt
            prompt = cfg.system_prompt
            mcp_prompts = [
                self._mcp_registry[n].prompt
                for n in cfg.mcp
                if self._mcp_registry[n].prompt
            ]
            if mcp_prompts:
                prompt += "\n\n## Werkzeuge\n\n"
                prompt += "Du hast Zugriff auf spezialisierte Tools. Nutze sie IMMER wenn sie passen — rate niemals Informationen die du nachschlagen kannst.\n\n"
                prompt += "\n\n".join(mcp_prompts)
                logger.info(
                    "[%s] %d MCP-Prompt(s) in System-Prompt injiziert",
                    cfg.name,
                    len(mcp_prompts),
                )
            options.system_prompt = prompt
        if mcp_servers:
            options.mcp_servers = mcp_servers
        if cfg.effort:
            options.effort = cfg.effort  # pyright: ignore[reportAttributeAccessIssue]
        if cfg.bare:
            options.bare = True  # pyright: ignore[reportAttributeAccessIssue]
        if cfg.tools:
            # "none" → empty string disables all built-in tools (MCP tools stay)
            options.tools = "" if cfg.tools == "none" else cfg.tools  # pyright: ignore[reportAttributeAccessIssue]

        return options

    @property
    def default_entity(self) -> str:
        return self._default_entity

    @property
    def configs(self) -> dict[str, EntityConfig]:
        return self._configs

    @property
    def failed(self) -> dict[str, str]:
        return dict(self._failed)

    def get(self, entity_name: str | None = None) -> tuple[SDKWrapper, EntityConfig]:
        """Resolve entity by name (or default). Raises KeyError if unknown."""
        name = entity_name or self._default_entity
        if name not in self._configs:
            raise KeyError(f"Unbekannte Entity: {name}")
        return self._wrappers[name], self._configs[name]

    def apply_settings(
        self,
        entity_name: str | None = None,
        *,
        model: str | None = None,
        effort: str | None = None,
    ) -> EntityConfig:
        """Update an entity's model/effort in memory and rebuild its SDK
        options so the next connect() picks them up. Does NOT reconnect or
        persist — the caller handles reconnect (so the running process adopts
        the change) and writing entities.toml (so it survives a restart).
        """
        name = entity_name or self._default_entity
        if name not in self._configs:
            raise KeyError(f"Unbekannte Entity: {name}")
        cfg = self._configs[name]
        if model is not None:
            cfg.model = model
        if effort is not None:
            cfg.effort = effort
        self._wrappers[name].update_options(self._build_options(cfg))
        return cfg

    async def startup(self) -> None:
        """Connect all entity SDKWrappers. Graceful degradation on failure."""
        for name, wrapper in self._wrappers.items():
            try:
                await wrapper.connect()
            except Exception as exc:
                logger.error("[%s] SDK connect fehlgeschlagen: %s", name, exc)
                self._failed[name] = str(exc)

    async def shutdown(self) -> None:
        """Disconnect all entity SDKWrappers."""
        for name, wrapper in self._wrappers.items():
            try:
                await wrapper.disconnect()
            except Exception:
                logger.debug("[%s] disconnect Fehler", name, exc_info=True)
