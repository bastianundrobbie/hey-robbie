"""Capability infrastructure for proactive server features.

Capabilities are server-side features that can:
1. Match voice commands directly (bypassing the LLM for speed)
2. Send proactive notifications through the voice WebSocket
   (e.g. "Timer abgelaufen!", "Wecker klingelt!")

Architecture:
    CapabilityManager holds all capabilities, runs a background notification
    loop, and integrates with the voice WebSocket in app.py.

    BaseCapability is the abstract base class. Each capability implements:
    - match_command(transcript) → CommandResult | None (fast keyword matching)
    - check_notifications() → list[Notification] (called every second)
    - startup() / shutdown() lifecycle hooks
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Notification:
    """A proactive notification to be delivered via voice."""

    text: str  # Text for TTS (Robbie speaks this)
    entity: str = "robbie"  # Which entity's TTS config to use
    behaviors: list[str] = field(
        default_factory=list
    )  # Optional [ROBBIE:xxx] behaviors
    source: str = ""  # Which capability sent this
    meta: dict = field(
        default_factory=dict
    )  # Source-specific payload (alarm ring mode: label/time/snooze_count)


@dataclass
class CommandResult:
    """Result of a matched voice command."""

    text: str  # Response text (spoken by TTS)
    behaviors: list[str] = field(default_factory=list)  # Optional behaviors
    # True = duplex ends the conversation after the reply (no follow-up
    # window). Used when the capability recycles the SDK session (model
    # switch) — a follow-up turn would race the reconnect.
    end_conversation: bool = False


class BaseCapability(ABC):
    """Abstract base class for server capabilities."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique capability name (e.g. 'timer', 'alarm')."""
        ...

    async def startup(self) -> None:
        """Called once when the server starts. Override for initialization."""
        pass

    async def shutdown(self) -> None:
        """Called once when the server stops. Override for cleanup."""
        pass

    def match_command(self, transcript: str) -> CommandResult | None:
        """Try to match a voice transcript to a command.

        Called for every voice turn BEFORE the LLM. If this returns a
        CommandResult, the result is spoken directly (no SDK call needed).
        Return None to pass through to the LLM.
        """
        return None

    async def check_notifications(self) -> list[Notification]:
        """Called every ~1 second. Return any pending notifications.

        Notifications are delivered proactively through the voice WebSocket
        (Robbie speaks them). Return an empty list if nothing is pending.
        """
        return []

    def get_status(self) -> list[dict[str, Any]]:
        """Return current status items for UI display.

        Each item is a dict with at least 'type' and display info.
        Return empty list if nothing to show.
        """
        return []

    def get_tool_description(self) -> str:
        """Return a description of this capability for the LLM system prompt.

        This is auto-injected into Robbie's prompt so the LLM knows
        what voice commands are available. Return empty string to skip.
        """
        return ""


class CapabilityManager:
    """Manages all capabilities and runs the notification loop.

    Integration points in app.py:
    1. lifespan: create manager, register capabilities, startup/shutdown
    2. voice_websocket: call match_command() before shortcuts/SDK
    3. notification delivery: consume from notification_queue, send via WS
    """

    def __init__(self) -> None:
        self._capabilities: list[BaseCapability] = []
        self._notification_queue: asyncio.Queue[Notification] = asyncio.Queue()
        self._loop_task: asyncio.Task | None = None
        self._running = False

    def register(self, capability: BaseCapability) -> None:
        """Register a capability. Must be called before startup()."""
        self._capabilities.append(capability)
        logger.info("[capabilities] Registered: %s", capability.name)

    @property
    def capabilities(self) -> list[BaseCapability]:
        return list(self._capabilities)

    async def startup(self) -> None:
        """Start all capabilities and the notification loop."""
        for cap in self._capabilities:
            try:
                await cap.startup()
                logger.info("[capabilities] %s started", cap.name)
            except Exception as exc:
                logger.error("[capabilities] %s startup failed: %s", cap.name, exc)

        self._running = True
        self._loop_task = asyncio.create_task(self._notification_loop())
        logger.info(
            "[capabilities] Notification loop started (%d capabilities)",
            len(self._capabilities),
        )

    async def shutdown(self) -> None:
        """Stop the notification loop and shut down all capabilities."""
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
            self._loop_task = None

        for cap in self._capabilities:
            try:
                await cap.shutdown()
            except Exception as exc:
                logger.error("[capabilities] %s shutdown failed: %s", cap.name, exc)

        logger.info("[capabilities] All capabilities shut down")

    def match_command(self, transcript: str) -> tuple[str, CommandResult] | None:
        """Try to match a transcript against all capabilities.

        Returns (capability_name, result) or None.
        Capabilities are checked in registration order.
        """
        for cap in self._capabilities:
            result = cap.match_command(transcript)
            if result is not None:
                return (cap.name, result)
        return None

    @property
    def notification_queue(self) -> asyncio.Queue[Notification]:
        """Queue of pending notifications for the voice delivery task."""
        return self._notification_queue

    def get_prompt_injection(self) -> str:
        """Build a combined tool description for LLM system prompt injection."""
        parts = []
        for cap in self._capabilities:
            desc = cap.get_tool_description()
            if desc:
                parts.append(desc)
        return "\n\n".join(parts)

    def get_status(self) -> dict[str, Any]:
        """Collect status from all capabilities for UI display."""
        timers = []
        alarms = []
        for cap in self._capabilities:
            items = cap.get_status()
            for item in items:
                if item.get("type") == "timer":
                    timers.append(item)
                elif item.get("type") == "alarm":
                    alarms.append(item)
        return {"timers": timers, "alarms": alarms}

    async def _notification_loop(self) -> None:
        """Background task: check capabilities for notifications every second."""
        while self._running:
            try:
                await asyncio.sleep(1.0)
                for cap in self._capabilities:
                    try:
                        notifications = await cap.check_notifications()
                        for n in notifications:
                            n.source = n.source or cap.name
                            await self._notification_queue.put(n)
                            logger.info(
                                "[capabilities] Notification from %s: %s",
                                cap.name,
                                n.text[:60],
                            )
                    except Exception as exc:
                        logger.error(
                            "[capabilities] %s check_notifications failed: %s",
                            cap.name,
                            exc,
                        )
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("[capabilities] Notification loop error: %s", exc)
                await asyncio.sleep(5.0)  # backoff on unexpected errors
