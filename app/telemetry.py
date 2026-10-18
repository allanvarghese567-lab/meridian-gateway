"""Latency accounting and Prometheus text exposition.

Crossing `alert_threshold_seconds` schedules a broadcast on the running
loop and returns immediately — the tool call does not wait on Slack.
"""

from __future__ import annotations

import asyncio
import threading
from typing import Any

from app.notifications import NotificationBroadcaster


class LatencyMonitor:
    def __init__(
        self,
        broadcaster: NotificationBroadcaster | None = None,
        alert_threshold_seconds: float = 2.0,
    ) -> None:
        if alert_threshold_seconds <= 0:
            raise ValueError("alert_threshold_seconds must be > 0")
        self.alert_threshold_seconds = alert_threshold_seconds
        self.broadcaster = broadcaster
        self._lock = threading.Lock()
        self._latency_sum = 0.0
        self._latency_count = 0
        self._alerts = 0
        self._calls: dict[tuple[str, str], int] = {}
        self._inflight = 0
        self._recent: list[dict[str, Any]] = []
        self._background: set[asyncio.Task[None]] = set()

    def enter(self) -> None:
        with self._lock:
            self._inflight += 1

    def exit(self) -> None:
        with self._lock:
            self._inflight = max(0, self._inflight - 1)

    async def observe(
        self,
        *,
        seconds: float,
        lane: str,
        outcome: str,
        tool: str,
        call_id: str,
    ) -> None:
        with self._lock:
            self._latency_sum += seconds
            self._latency_count += 1
            key = (lane, outcome)
            self._calls[key] = self._calls.get(key, 0) + 1
            self._recent.append(
                {"seconds": round(seconds, 6), "lane": lane, "outcome": outcome, "tool": tool}
            )
            if len(self._recent) > 60:
                del self._recent[:-60]
        if seconds < self.alert_threshold_seconds or self.broadcaster is None:
            return
        with self._lock:
            self._alerts += 1
        event = {
            "tool": tool,
            "lane": lane,
            "duration_seconds": round(seconds, 4),
            "call_id": call_id,
            "outcome": outcome,
        }
        task = asyncio.create_task(self.broadcaster.publish_spike(event))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            average = (self._latency_sum / self._latency_count) if self._latency_count else 0.0
            return {
                "calls": dict(self._calls),
                "average_latency_seconds": average,
                "alerts": self._alerts,
                "inflight": self._inflight,
                "observations": self._latency_count,
                "recent": list(self._recent),
            }

    def render_prometheus(self, *, cache_entries: int, cache_hits: int, cache_misses: int) -> str:
        snap = self.snapshot()
        lines: list[str] = [
            "# HELP gateway_up Gateway process is serving.",
            "# TYPE gateway_up gauge",
            "gateway_up 1",
            "# HELP gateway_calls_total Tool calls by lane and outcome.",
            "# TYPE gateway_calls_total counter",
        ]
        if not snap["calls"]:
            lines.append('gateway_calls_total{lane="internal",outcome="ok"} 0')
        else:
            for (lane, outcome), count in sorted(snap["calls"].items()):
                lines.append(
                    f'gateway_calls_total{{lane="{lane}",outcome="{outcome}"}} {count}'
                )
        average = snap["average_latency_seconds"]
        lines.extend(
            [
                "# HELP gateway_average_latency_seconds Mean tool execution latency.",
                "# TYPE gateway_average_latency_seconds gauge",
                f"gateway_average_latency_seconds {average:.6f}",
                "# HELP gateway_inflight Tool calls currently inside a lane semaphore.",
                "# TYPE gateway_inflight gauge",
                f"gateway_inflight {snap['inflight']}",
                "# HELP gateway_alerts_total Latency threshold crossings.",
                "# TYPE gateway_alerts_total counter",
                f"gateway_alerts_total {snap['alerts']}",
                "# HELP gateway_cache_entries Resident LRU entries.",
                "# TYPE gateway_cache_entries gauge",
                f"gateway_cache_entries {cache_entries}",
                "# HELP gateway_cache_hits_total Cache hits.",
                "# TYPE gateway_cache_hits_total counter",
                f"gateway_cache_hits_total {cache_hits}",
                "# HELP gateway_cache_misses_total Cache misses.",
                "# TYPE gateway_cache_misses_total counter",
                f"gateway_cache_misses_total {cache_misses}",
            ]
        )
        return "\n".join(lines) + "\n"
