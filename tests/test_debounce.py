"""Cooldown debouncer: first spike immediate, the rest collapse."""

from __future__ import annotations

import asyncio
import os
import unittest

from app.notifications import NotificationBroadcaster


class TestDebounce(unittest.IsolatedAsyncioTestCase):
    async def test_groups_followup_spikes(self) -> None:
        os.environ.pop("SLACK_WEBHOOK_URL", None)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)
        broadcaster = NotificationBroadcaster(cooldown_seconds=0.05)
        queue = broadcaster.subscribe()
        await broadcaster.publish_spike(
            {"tool": "external_fx", "lane": "external", "duration_seconds": 2.2, "call_id": "a"}
        )
        await broadcaster.publish_spike(
            {"tool": "external_search", "lane": "external", "duration_seconds": 2.5, "call_id": "b"}
        )
        await broadcaster.publish_spike(
            {"tool": "external_fx", "lane": "external", "duration_seconds": 3.1, "call_id": "c"}
        )
        first = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(first["kind"], "spike")
        self.assertEqual(first["tool"], "external_fx")
        summary = await asyncio.wait_for(queue.get(), timeout=1)
        self.assertEqual(summary["kind"], "summary")
        self.assertEqual(summary["count"], 2)
        self.assertIn("external_search", summary["text"])
        self.assertIn("•", summary["text"])
        await broadcaster.shutdown()


if __name__ == "__main__":
    unittest.main()
