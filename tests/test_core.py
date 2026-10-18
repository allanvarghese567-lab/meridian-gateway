"""Stdlib tests for redaction and the LRU."""

from __future__ import annotations

import threading
import unittest

from app.cache import GatewayLRUCache, build_cache_key
from app.security import REDACTED, deep_scrub, scrub_string


class TestScrub(unittest.TestCase):
    def test_whitelist_passthrough_and_sensitive_keys(self) -> None:
        raw = {
            "subject": "Bearer should-stay-in-subject",
            "message": "Ship the crate.",
            "description": {"note": "Bearer also-stays-because-parent-is-whitelisted"},
            "api_key": "sk-live-abcdefghij123456",
            "Password": "hunter2",
            "creditCardNumber": "4242424242424242",
            "notes": "Authorization: Bearer abcdefghijklmnop",
            "nested": [{"city": "Kochi", "ssn": "123-45-6789"}],
            "pan_in_text": "card 4242424242424242 on file",
        }
        out = deep_scrub(raw)
        self.assertEqual(out["subject"], "Bearer should-stay-in-subject")
        self.assertEqual(out["message"], "Ship the crate.")
        self.assertIn("Bearer", out["description"]["note"])
        self.assertEqual(out["api_key"], REDACTED)
        self.assertEqual(out["Password"], REDACTED)
        self.assertEqual(out["creditCardNumber"], REDACTED)
        self.assertNotIn("abcdefghijklmnop", out["notes"])
        self.assertIn(REDACTED, out["notes"])
        self.assertEqual(out["nested"][0]["ssn"], REDACTED)
        self.assertEqual(out["nested"][0]["city"], "Kochi")
        self.assertNotIn("4242424242424242", out["pan_in_text"])

    def test_token_pattern_on_raw_strings(self) -> None:
        cleaned = scrub_string("prefix eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature tail")
        self.assertNotIn("eyJhbGci", cleaned)
        self.assertIn(REDACTED, cleaned)

    def test_lists_are_walked(self) -> None:
        out = deep_scrub([{"token": "abc"}, "safe"])
        self.assertEqual(out[0]["token"], REDACTED)
        self.assertEqual(out[1], "safe")


class TestCache(unittest.TestCase):
    def test_key_is_digest_of_sanitized_input(self) -> None:
        first = build_cache_key("internal_lookup", {"sku": "A", "api_key": "one-secret"})
        second = build_cache_key("internal_lookup", {"sku": "A", "api_key": "other-secret"})
        self.assertEqual(first, second)
        self.assertNotIn("secret", first)
        self.assertEqual(len(first), 64)

    def test_lru_evicts_oldest(self) -> None:
        cache = GatewayLRUCache(max_size=2, default_ttl_seconds=30)
        cache.set("a", 1)
        cache.set("b", 2)
        self.assertEqual(cache.get("a"), 1)
        cache.set("c", 3)
        self.assertIsNone(cache.get("b"))
        self.assertEqual(cache.get("c"), 3)

    def test_ttl(self) -> None:
        now = {"t": 0.0}
        cache = GatewayLRUCache(max_size=10, default_ttl_seconds=5, clock=lambda: now["t"])
        cache.set("a", {"ok": True})
        now["t"] = 5.0
        self.assertIsNone(cache.get("a"))

    def test_concurrent_sets(self) -> None:
        cache = GatewayLRUCache(max_size=100, default_ttl_seconds=30)
        errors: list[BaseException] = []

        def worker(index: int) -> None:
            try:
                for step in range(50):
                    cache.set(f"{index}-{step % 10}", step)
                    cache.get(f"{index}-{step % 10}")
            except BaseException as exc:  # noqa: BLE001 — surface any thread failure
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertLessEqual(len(cache), 100)


if __name__ == "__main__":
    unittest.main()
