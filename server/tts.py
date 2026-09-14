"""TTS Manager — Cartesia Sonic TTS pool management.

Extracted from ``app.py`` to reduce module-level globals.  Cartesia Sonic
TTS uses persistent session pools exposed via :meth:`get_cartesia_pool`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from server.cartesia_tts import CartesiaTTS

if TYPE_CHECKING:
    from server.entities import EntityConfig

logger = logging.getLogger("robbie-server")


class TTSManager:
    """Centralised Cartesia Sonic TTS pool management."""

    CARTESIA_POOL_SIZE = 3  # parallel Cartesia Sonic TTS sessions per entity

    # ── construction ────────────────────────────────────────────

    def __init__(self) -> None:
        # Cartesia Sonic TTS
        self.cartesia_api_key: str = ""
        self._cartesia_pools: dict[str, list[CartesiaTTS]] = {}

    # ── configuration (called once from lifespan) ───────────────

    def configure(self, *, cartesia_key: str = "") -> None:
        """Resolve API keys and log availability."""
        self.cartesia_api_key = cartesia_key

        if self.cartesia_api_key:
            logger.info("[tts] CARTESIA_API_KEY loaded (Cartesia Sonic TTS available)")
        else:
            logger.info("[tts] CARTESIA_API_KEY not set (Cartesia TTS disabled)")

    # ── Cartesia Sonic TTS pool management ──────────────────────

    async def connect_cartesia_pools(self, entity_configs: list[EntityConfig]) -> None:
        """Connect Cartesia Sonic TTS session pools for entities using tts_provider='cartesia'."""
        if not self.cartesia_api_key:
            return
        for cfg in entity_configs:
            if cfg.tts_provider.lower() != "cartesia":
                continue
            voice = cfg.tts_voice_id or "a0e99841-438c-4a64-b679-ae501e7d6091"
            model = cfg.tts_model or "sonic-3"
            pool: list[CartesiaTTS] = []
            for i in range(self.CARTESIA_POOL_SIZE):
                tts_session = CartesiaTTS(
                    api_key=self.cartesia_api_key,
                    voice_id=voice,
                    model=model,
                    language="de",
                    speed=cfg.tts_speed,
                )
                try:
                    await tts_session.connect()
                    pool.append(tts_session)
                except Exception as exc:
                    logger.error(
                        "[%s] Cartesia TTS pool[%d] connect failed: %s",
                        cfg.name,
                        i,
                        exc,
                    )
            if pool:
                self._cartesia_pools[cfg.name] = pool
                logger.info(
                    "[%s] Cartesia Sonic TTS pool ready (%d sessions, voice=%s)",
                    cfg.name,
                    len(pool),
                    voice,
                )

    async def disconnect_cartesia_pools(self) -> None:
        """Disconnect all Cartesia Sonic TTS pools."""
        for name, pool in self._cartesia_pools.items():
            for session in pool:
                try:
                    await session.disconnect()
                except Exception:
                    logger.debug(
                        "[%s] Cartesia TTS disconnect error", name, exc_info=True
                    )
        self._cartesia_pools.clear()

    def get_cartesia_pool(self, entity_name: str) -> list[CartesiaTTS] | None:
        """Get the Cartesia TTS session pool for an entity, or None."""
        pool = self._cartesia_pools.get(entity_name)
        return pool if pool else None
