"""Latency-alert broadcaster.

The first spike in a cooldown window is dispatched immediately. Later
spikes are held and emitted together as one bulleted summary when the
timer expires. The window does not extend — continuous noise still
collapses to a single follow-up.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("gateway.notifications")

_MAX_BUFFER = 100


def _bullet(event: dict[str, Any]) -> str:
    tool = event.get("tool", "unknown")
    lane = event.get("lane", "external")
    seconds = float(event.get("duration_seconds", 0.0))
    call_id = event.get("call_id", "")
    return f"• `{tool}` · {lane} · {seconds:.3f}s · {call_id}"


def format_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    shown = events[:_MAX_BUFFER]
    lines = [_bullet(event) for event in shown]
    omitted = max(0, len(events) - len(shown))
    if omitted:
        lines.append(f"• … {omitted} more")
    text = "Gateway latency summary\n" + "\n".join(lines)
    return {
        "kind": "summary",
        "count": len(events),
        "spikes": shown,
        "text": text,
    }


class NotificationBroadcaster:
    def __init__(self, cooldown_seconds: float = 10.0) -> None:
        if cooldown_seconds <= 0:
            raise ValueError("cooldown_seconds must be > 0")
        self.cooldown_seconds = cooldown_seconds
        self._subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self._lock = asyncio.Lock()
        self._window_open = False
        self._window_deadline = 0.0
        self._buffer: list[dict[str, Any]] = []
        self._timer: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._closed = False

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=200)
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        try:
            self._subscribers.remove(queue)
        except ValueError:
            return

    def debounce_view(self) -> dict[str, float | int | bool]:
        remaining = 0.0
        if self._window_open:
            remaining = max(0.0, self._window_deadline - time.monotonic())
        return {
            "open": self._window_open,
            "buffered": len(self._buffer),
            "remaining_seconds": round(remaining, 3),
        }

    async def publish_spike(self, event: dict[str, Any]) -> None:
        """Leading edge goes out now; the rest wait for the cooldown."""
        payload = {"kind": "spike", **event}
        dispatch_now = False
        async with self._lock:
            if self._closed:
                return
            if not self._window_open:
                self._window_open = True
                loop = asyncio.get_running_loop()
                self._window_deadline = time.monotonic() + self.cooldown_seconds
                self._timer = loop.create_task(self._flush_later())
                dispatch_now = True
            else:
                if len(self._buffer) < _MAX_BUFFER:
                    self._buffer.append(payload)
                return
        if dispatch_now:
            await self._fanout(payload)
            self._spawn_webhook(payload)

    async def _flush_later(self) -> None:
        try:
            await asyncio.sleep(self.cooldown_seconds)
            async with self._lock:
                batch = self._buffer
                self._buffer = []
                self._window_open = False
                self._window_deadline = 0.0
                self._timer = None
            if not batch:
                return
            summary = format_summary(batch)
            await self._fanout(summary)
            self._spawn_webhook(summary)
        except asyncio.CancelledError:
            raise

    async def _fanout(self, payload: dict[str, Any]) -> None:
        stale: list[asyncio.Queue[dict[str, Any]]] = []
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:
                stale.append(queue)
        for queue in stale:
            self.unsubscribe(queue)

    def _spawn_webhook(self, payload: dict[str, Any]) -> None:
        task = asyncio.create_task(self._post_webhooks(payload))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _post_webhooks(self, payload: dict[str, Any]) -> None:
        slack = os.getenv("SLACK_WEBHOOK_URL", "").strip()
        discord = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
        if not slack and not discord:
            return
        text = str(payload.get("text") or _fallback_text(payload))
        slack_body = _slack_body(payload, text)
        discord_body = {"content": text[:1900]}
        timeout = httpx.Timeout(3.0, connect=2.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if slack:
                await _safe_post(client, slack, slack_body)
            if discord:
                await _safe_post(client, discord, discord_body)

    async def shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            timer = self._timer
            self._timer = None
            self._window_open = False
            self._buffer = []
        if timer is not None:
            timer.cancel()
        for task in list(self._background):
            task.cancel()
        self._subscribers.clear()


def _fallback_text(payload: dict[str, Any]) -> str:
    if payload.get("kind") == "spike":
        return _bullet(payload)
    return "Gateway latency alert"


def _slack_body(payload: dict[str, Any], text: str) -> dict[str, Any]:
    if payload.get("kind") == "summary":
        return {
            "text": text,
            "blocks": [
                {
                    "type": "header",
                    "text": {"type": "plain_text", "text": "Gateway latency summary"},
                },
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": text[:2900]},
                },
            ],
        }
    return {
        "text": text,
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Latency spike*\n{text}",
                },
            }
        ],
    }


async def _safe_post(client: httpx.AsyncClient, url: str, body: dict[str, Any]) -> None:
    try:
        response = await client.post(url, json=body)
        if response.status_code >= 300:
            logger.warning("webhook returned %s", response.status_code)
    except httpx.HTTPError:
        logger.warning("webhook delivery failed")
