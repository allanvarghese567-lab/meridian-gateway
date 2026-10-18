"""Thundering-herd probe.

Fires concurrent batches at /agent/execute-tools. The first wave includes
duplicate lookups (single-flight + cache) and slow external calls that
cross the latency threshold. A second wave repeats the lookup and should
return from cache in well under a millisecond of gateway work.

    pip install httpx
    python scripts/stress_test.py
    GATEWAY_URL=http://127.0.0.1 python scripts/stress_test.py
"""

from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time

import httpx

BASE = os.getenv("GATEWAY_URL", "http://127.0.0.1").rstrip("/")
SLOW_MS = int(os.getenv("STRESS_DELAY_MS", "2300"))


def _lookup(sku: str) -> dict:
    return {
        "tools": [
            {
                "name": "internal_lookup",
                "arguments": {"sku": sku, "description": "Warehouse bin check"},
            }
        ]
    }


def _slow(pair: str) -> dict:
    return {
        "tools": [
            {
                "name": "external_fx",
                "arguments": {
                    "pair": pair,
                    "delay_ms": SLOW_MS,
                    "api_key": "sk-live-should-never-be-stored",
                    "message": "desk check, leave this prose alone",
                },
            }
        ]
    }


async def _post(client: httpx.AsyncClient, payload: dict) -> tuple[float, dict]:
    started = time.perf_counter()
    response = await client.post(f"{BASE}/agent/execute-tools", json=payload)
    elapsed_ms = (time.perf_counter() - started) * 1000
    response.raise_for_status()
    return elapsed_ms, response.json()


async def main() -> int:
    timeout = httpx.Timeout(30.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        health = await client.get(f"{BASE}/healthz")
        health.raise_for_status()
        print(f"health {health.status_code} {health.json()}")

        wave_one = [
            _lookup("MX-204"),
            _lookup("MX-204"),
            _lookup("MX-204"),
            _slow("USDINR"),
            _slow("EURUSD"),
        ]
        started = time.perf_counter()
        first = await asyncio.gather(*(_post(client, item) for item in wave_one))
        print(f"\nwave 1 wall {((time.perf_counter() - started) * 1000):.1f} ms")
        for elapsed, body in first:
            result = body["results"][0]
            print(
                f"  {result['name']:16} lane={result['lane']:8} "
                f"http={elapsed:8.1f} ms tool={result['duration_ms']:8.2f} ms "
                f"cache={result['cache_hit']} coalesced={result['coalesced']} "
                f"scrubbed_api_key={result['scrubbed_arguments'].get('api_key', '-')}"
            )

        second_payloads = [_lookup("MX-204") for _ in range(6)]
        second = await asyncio.gather(*(_post(client, item) for item in second_payloads))
        tool_times = [body["results"][0]["duration_ms"] for _, body in second]
        hits = sum(1 for _, body in second if body["results"][0]["cache_hit"])
        print(
            f"\nwave 2 cache hits {hits}/{len(second)} "
            f"tool ms min={min(tool_times):.3f} median={statistics.median(tool_times):.3f}"
        )
        metrics = await client.get(f"{BASE}/metrics")
        metrics.raise_for_status()
        interesting = [
            line
            for line in metrics.text.splitlines()
            if line.startswith("gateway_") and not line.startswith("gateway_calls_total{")
        ]
        print("\nmetrics")
        for line in interesting:
            if line.startswith("gateway_calls_total") or line.startswith("#"):
                continue
            print(f"  {line}")
        calls = [line for line in metrics.text.splitlines() if line.startswith("gateway_calls_total{")]
        for line in calls:
            print(f"  {line}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except httpx.HTTPError as exc:
        print(f"request failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
