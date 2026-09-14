"""Cartesia Sonic — Streaming TTS wrapper.

Maintains a persistent WebSocket session to the Cartesia Sonic API and
synthesizes text to streaming PCM16 audio chunks (24kHz mono).

Usage::

    tts = CartesiaTTS(api_key="...", voice_id="a0e99841-438c-4a64-b679-ae501e7d6091")
    await tts.connect()

    async for chunk in tts.synthesize("Hallo, ich bin Robbie!"):
        # chunk is bytes — raw PCM16 24kHz mono
        ws.send_bytes(chunk)

    await tts.disconnect()
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from typing import AsyncIterator

import aiohttp

logger = logging.getLogger("robbie-server")

# Default model for Cartesia Sonic TTS
DEFAULT_MODEL = "sonic-3"

# Default language
DEFAULT_LANGUAGE = "de"

# Cartesia WebSocket API version
_API_VERSION = "2025-04-16"

# WebSocket endpoint
_WS_URL = "wss://api.cartesia.ai/tts/websocket"

# Output format: raw PCM16 little-endian, 24kHz mono
_OUTPUT_FORMAT = {
    "container": "raw",
    "encoding": "pcm_s16le",
    "sample_rate": 24000,
}


class CartesiaTTS:
    """Streaming TTS via Cartesia Sonic WebSocket API.

    Maintains a persistent WebSocket connection.  Each ``synthesize()``
    call sends text and yields PCM16 audio chunks as they stream back.

    Parameters
    ----------
    api_key:
        Cartesia API key.
    voice_id:
        Voice UUID (e.g. "a0e99841-438c-4a64-b679-ae501e7d6091").
    model:
        Cartesia model ID (default: "sonic-3").
    language:
        Language code (default: "de").
    """

    def __init__(
        self,
        api_key: str,
        voice_id: str,
        model: str = DEFAULT_MODEL,
        language: str = DEFAULT_LANGUAGE,
        speed: float = 1.0,
    ) -> None:
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model
        self._language = language
        self._speed = max(0.6, min(1.5, speed))  # clamp to valid range

        self._session: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._connected = False
        self._lock = asyncio.Lock()  # serialize synthesize calls
        self._keepalive_task: asyncio.Task[None] | None = None

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def voice_id(self) -> str:
        return self._voice_id

    async def connect(self) -> None:
        """Open a persistent Cartesia Sonic WebSocket session."""
        if self._connected:
            return

        url = f"{_WS_URL}?api_key={self._api_key}&cartesia_version={_API_VERSION}"

        t0 = time.monotonic()
        self._session = aiohttp.ClientSession()
        try:
            self._ws = await self._session.ws_connect(
                url,
                heartbeat=30.0,
            )
        except aiohttp.WSServerHandshakeError as exc:
            # Never let the URL (which carries the API key) reach the logs.
            await self._session.close()
            self._session = None
            hint = " (402: no credit on the Cartesia account?)" if exc.status == 402 else ""
            raise RuntimeError(
                f"Cartesia handshake failed: HTTP {exc.status} {exc.message}{hint}"
            ) from None
        except Exception:
            await self._session.close()
            self._session = None
            raise
        elapsed = int((time.monotonic() - t0) * 1000)
        self._connected = True
        self._keepalive_task = asyncio.create_task(self._keepalive_loop())
        logger.info(
            "[Cartesia TTS] Connected (%s, voice=%s, lang=%s, %dms)",
            self._model,
            self._voice_id,
            self._language,
            elapsed,
        )

    async def disconnect(self) -> None:
        """Close the Cartesia WebSocket session."""
        if not self._connected:
            return
        self._connected = False
        if self._keepalive_task and not self._keepalive_task.done():
            self._keepalive_task.cancel()
            self._keepalive_task = None
        try:
            if self._ws and not self._ws.closed:
                await self._ws.close()
        except Exception as e:
            logger.debug("[Cartesia TTS] disconnect ws error: %s", e)
        try:
            if self._session and not self._session.closed:
                await self._session.close()
        except Exception as e:
            logger.debug("[Cartesia TTS] disconnect session error: %s", e)
        self._ws = None
        self._session = None
        logger.info("[Cartesia TTS] Disconnected")

    async def reconnect(self) -> None:
        """Disconnect and reconnect (e.g. after errors)."""
        await self.disconnect()
        await self.connect()

    async def _keepalive_loop(self) -> None:
        """Send periodic WebSocket pings to prevent Cartesia idle disconnect.

        Cartesia closes idle WebSocket connections after ~30-60s.  The
        aiohttp ``heartbeat`` parameter sends protocol-level pings, but
        Cartesia may ignore those.  This loop sends an application-level
        WebSocket ping every 15s to keep the connection alive.
        """
        try:
            while self._connected and self._ws and not self._ws.closed:
                await asyncio.sleep(15)
                if self._ws and not self._ws.closed and self._connected:
                    try:
                        await self._ws.ping()
                    except Exception:
                        # Connection died — mark as disconnected so next
                        # synthesize() triggers a reconnect.
                        logger.debug("[Cartesia TTS] Keepalive ping failed")
                        self._connected = False
                        break
        except asyncio.CancelledError:
            pass

    async def synthesize(
        self,
        text: str,
        context_id: str | None = None,
        continue_context: bool = False,
    ) -> AsyncIterator[bytes]:
        """Send text and yield streaming PCM16 audio chunks.

        Each chunk is raw PCM16, 24kHz, mono (2 bytes per sample).
        Audio data arrives base64-encoded and is decoded before yielding.

        Parameters
        ----------
        context_id:
            Shared context for Continuations.  When multiple sentences
            share the same ``context_id``, Cartesia maintains prosody
            across them — no seams between sentences in the same turn.
            If ``None``, a fresh UUID is generated (standalone mode).
        continue_context:
            If ``True``, this request continues an existing context
            (prosody carries over from the previous sentence).  Set to
            ``True`` for all sentences after the first in a turn.

        This method is serialized — only one synthesize() call runs
        at a time.  Concurrent calls wait for the lock.
        """
        # Retry once on dead WebSocket (idle timeout, server disconnect).
        # First attempt uses existing connection; on transport error we
        # reconnect and retry exactly once.
        for attempt in range(2):
            if not self._connected or not self._ws or self._ws.closed:
                if attempt == 0:
                    logger.warning(
                        "[Cartesia TTS] Not connected, attempting reconnect..."
                    )
                await self.connect()

            async with self._lock:
                t0 = time.monotonic()
                first_chunk = True
                total_bytes = 0

                # Use caller-provided context_id for Continuations (natural
                # prosody across sentences in the same turn), or generate a
                # fresh one for standalone synthesis.
                ctx = context_id or str(uuid.uuid4())

                try:
                    request = {
                        "model_id": self._model,
                        "transcript": text,
                        "voice": {"mode": "id", "id": self._voice_id},
                        "language": self._language,
                        "context_id": ctx,
                        "output_format": _OUTPUT_FORMAT,
                        "continue": continue_context,
                    }
                    if self._speed != 1.0:
                        request["generation_config"] = {"speed": self._speed}  # type: ignore[assignment]

                    assert self._ws is not None
                    await self._ws.send_json(request)

                    async for msg in self._ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            response = json.loads(msg.data)

                            # Filter by context_id — ignore stale responses
                            if response.get("context_id") != ctx:
                                continue

                            msg_type = response.get("type")

                            if msg_type == "error":
                                error_msg = response.get("error", "unknown error")
                                status = response.get("status_code", "???")
                                logger.error(
                                    '[Cartesia TTS] Error (status %s): %s — text: "%s..."',
                                    status,
                                    error_msg,
                                    text[:40],
                                )
                                self._connected = False
                                raise RuntimeError(
                                    f"Cartesia TTS error ({status}): {error_msg}"
                                )

                            if msg_type == "chunk":
                                b64_data = response.get("data")
                                if b64_data:
                                    chunk = base64.b64decode(b64_data)
                                    total_bytes += len(chunk)
                                    if first_chunk:
                                        ttfb = int((time.monotonic() - t0) * 1000)
                                        logger.info(
                                            '[Cartesia TTS] "%s..." TTFB=%dms%s',
                                            text[:40],
                                            ttfb,
                                            " (retry)" if attempt > 0 else "",
                                        )
                                        first_chunk = False
                                    yield chunk

                            if msg_type == "done" or response.get("done") is True:
                                break

                        elif msg.type in (
                            aiohttp.WSMsgType.CLOSED,
                            aiohttp.WSMsgType.CLOSING,
                            aiohttp.WSMsgType.ERROR,
                        ):
                            logger.error(
                                "[Cartesia TTS] WebSocket closed unexpectedly: %s",
                                msg.type,
                            )
                            self._connected = False
                            raise ConnectionError(
                                f"Cartesia WebSocket closed: {msg.type}"
                            )

                    elapsed = int((time.monotonic() - t0) * 1000)
                    audio_dur = total_bytes / (24000 * 2)
                    logger.info(
                        '[Cartesia TTS] "%s..." → %.1fs audio (%d bytes, %dms total)',
                        text[:40],
                        audio_dur,
                        total_bytes,
                        elapsed,
                    )
                    return  # success — exit retry loop

                except (ConnectionError, OSError) as e:
                    # Transport errors (dead WebSocket) — retry once
                    self._connected = False
                    if attempt == 0:
                        logger.warning(
                            '[Cartesia TTS] "%s..." transport error, reconnecting and retrying: %s',
                            text[:40],
                            e,
                        )
                        await self.reconnect()
                        continue  # retry
                    logger.error("[Cartesia TTS] synthesize failed after retry: %s", e)
                    raise

                except RuntimeError:
                    raise  # API errors (429 etc.) — don't retry

                except Exception as e:
                    logger.error("[Cartesia TTS] synthesize error: %s", e)
                    self._connected = False
                    raise
