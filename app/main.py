"""FastAPI gateway: parallel tool lanes, scrubbed cache, live alerts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, field_validator

from app.cache import GatewayLRUCache, build_cache_key
from app.notifications import NotificationBroadcaster
from app.security import deep_scrub
from app.telemetry import LatencyMonitor

logger = logging.getLogger("gateway")

_NAME_LIMIT = 128
_MAX_TOOLS = 32
_MAX_ARGUMENT_BYTES = 32_768
_CACHE_BYPASS = frozenset({"internal_clock"})
_FX = {"USDINR": 83.42, "EURUSD": 1.08, "GBPUSD": 1.27}


class ToolError(Exception):
    """A single tool failed; sibling calls still complete."""


class ToolCallPayload(BaseModel):
    name: str = Field(min_length=1, max_length=_NAME_LIMIT)
    arguments: dict[str, Any] = Field(default_factory=dict)
    call_id: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        if not value[0].isalpha():
            raise ValueError("tool name must start with a letter")
        allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-")
        if any(char not in allowed for char in value):
            raise ValueError("tool name has illegal characters")
        return value

    @field_validator("arguments")
    @classmethod
    def _arguments_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(value, default=str)
        if len(encoded.encode("utf-8")) > _MAX_ARGUMENT_BYTES:
            raise ValueError("arguments exceed 32KiB")
        return value


class ExecuteRequest(BaseModel):
    tools: list[ToolCallPayload] = Field(min_length=1, max_length=_MAX_TOOLS)


class ToolCallResult(BaseModel):
    call_id: str
    name: str
    lane: Literal["internal", "external"]
    ok: bool
    cache_hit: bool
    coalesced: bool
    duration_ms: float
    scrubbed_arguments: Any
    result: dict[str, Any] | None = None
    error: str | None = None


class ExecuteResponse(BaseModel):
    request_id: str
    results: list[ToolCallResult]
    wall_ms: float


def _from_stored(
    stored: dict[str, Any],
    *,
    call_id: str,
    cache_hit: bool,
    coalesced: bool,
    duration_ms: float,
) -> ToolCallResult:
    data = dict(stored)
    data.update(
        {
            "call_id": call_id,
            "cache_hit": cache_hit,
            "coalesced": coalesced,
            "duration_ms": duration_ms,
        }
    )
    return ToolCallResult.model_validate(data)


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _delay_seconds(name: str, arguments: dict[str, Any]) -> float:
    raw = arguments.get("delay_ms", None)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return 0.04 if name.startswith("internal_") else 0.08
    return min(max(float(raw), 0.0), 8_000.0) / 1_000.0


async def invoke_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """In-process adapters. Secrets never leave via the returned document."""
    await asyncio.sleep(_delay_seconds(name, arguments))
    safe = deep_scrub(arguments)
    if name == "internal_sum":
        values = arguments.get("values", [])
        if not isinstance(values, list) or any(
            isinstance(item, bool) or not isinstance(item, (int, float)) for item in values
        ):
            raise ToolError("values must be a list of numbers")
        return {"sum": float(sum(values)), "count": len(values)}
    if name == "internal_lookup":
        sku = str(arguments.get("sku", ""))
        digest = hashlib.sha256(sku.encode("utf-8")).hexdigest()
        return {
            "sku": sku,
            "aisle": int(digest[:2], 16) % 40 + 1,
            "bin": int(digest[2:4], 16) % 90 + 1,
            "description": safe.get("description"),
        }
    if name == "internal_clock":
        return {"utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    if name == "external_fx":
        pair = str(arguments.get("pair", "")).upper()
        if pair in _FX:
            rate = _FX[pair]
        else:
            rate = round(int(hashlib.sha256(pair.encode("utf-8")).hexdigest()[:4], 16) % 5000 / 100, 2)
        return {"pair": pair, "rate": rate, "source": "desk"}
    if name == "external_search":
        query = safe.get("query", "")
        return {"query": query, "hits": []}
    return {"accepted": True, "tool": name, "arguments": safe}


class ToolOrchestrator:
    def __init__(
        self,
        cache: GatewayLRUCache,
        telemetry: LatencyMonitor,
        internal_permits: int = 15,
        external_permits: int = 5,
    ) -> None:
        self.cache = cache
        self.telemetry = telemetry
        self._internal = asyncio.Semaphore(internal_permits)
        self._external = asyncio.Semaphore(external_permits)
        self._flight_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Future[ToolCallResult]] = {}

    async def execute(self, calls: list[ToolCallPayload]) -> list[ToolCallResult]:
        results = await asyncio.gather(*(self._run(call) for call in calls), return_exceptions=True)
        settled: list[ToolCallResult] = []
        for call, result in zip(calls, results, strict=True):
            if isinstance(result, ToolCallResult):
                settled.append(result)
                continue
            logger.exception("tool lane failed", exc_info=result)
            settled.append(
                ToolCallResult(
                    call_id=call.call_id or uuid.uuid4().hex[:12],
                    name=call.name,
                    lane="internal" if call.name.startswith("internal_") else "external",
                    ok=False,
                    cache_hit=False,
                    coalesced=False,
                    duration_ms=0.0,
                    scrubbed_arguments=deep_scrub(call.arguments),
                    error="internal error",
                )
            )
        return settled

    async def _run(self, call: ToolCallPayload) -> ToolCallResult:
        lane: Literal["internal", "external"] = (
            "internal" if call.name.startswith("internal_") else "external"
        )
        call_id = call.call_id or uuid.uuid4().hex[:12]
        key = build_cache_key(call.name, call.arguments)
        bypass = call.name in _CACHE_BYPASS

        async with self._flight_lock:
            if not bypass:
                cached = self.cache.get(key)
                if isinstance(cached, dict):
                    return _from_stored(
                        cached,
                        call_id=call_id,
                        cache_hit=True,
                        coalesced=False,
                        duration_ms=0.05,
                    )
            leader = self._inflight.get(key)
            if leader is not None:
                waiter: asyncio.Future[ToolCallResult] | None = leader
                created: asyncio.Future[ToolCallResult] | None = None
            else:
                waiter = None
                created = asyncio.get_running_loop().create_future()
                self._inflight[key] = created

        if waiter is not None:
            shared = await asyncio.shield(waiter)
            return shared.model_copy(update={"coalesced": True, "cache_hit": False, "call_id": call_id})

        assert created is not None
        try:
            result = await self._leader(call, lane, call_id, key, bypass)
            if not created.done():
                created.set_result(result)
            return result
        except Exception as exc:
            if not created.done():
                created.set_exception(exc)
            raise
        finally:
            async with self._flight_lock:
                if self._inflight.get(key) is created:
                    del self._inflight[key]

    async def _leader(
        self,
        call: ToolCallPayload,
        lane: Literal["internal", "external"],
        call_id: str,
        key: str,
        bypass: bool,
    ) -> ToolCallResult:
        semaphore = self._internal if lane == "internal" else self._external
        async with semaphore:
            self.telemetry.enter()
            started = time.perf_counter()
            try:
                if not bypass:
                    cached = self.cache.get(key)
                    if isinstance(cached, dict):
                        return _from_stored(
                            cached,
                            call_id=call_id,
                            cache_hit=True,
                            coalesced=False,
                            duration_ms=round((time.perf_counter() - started) * 1000, 3),
                        )
                try:
                    output = await invoke_tool(call.name, call.arguments)
                    ok = True
                    error = None
                    outcome = "ok"
                except ToolError as exc:
                    output = None
                    ok = False
                    error = str(exc)
                    outcome = "error"
                except Exception:
                    logger.exception("tool %s failed", call.name)
                    output = None
                    ok = False
                    error = "tool failed"
                    outcome = "error"
            finally:
                elapsed = time.perf_counter() - started
                self.telemetry.exit()
        await self.telemetry.observe(
            seconds=elapsed,
            lane=lane,
            outcome=outcome,
            tool=call.name,
            call_id=call_id,
        )
        result = ToolCallResult(
            call_id=call_id,
            name=call.name,
            lane=lane,
            ok=ok,
            cache_hit=False,
            coalesced=False,
            duration_ms=round(elapsed * 1000, 3),
            scrubbed_arguments=deep_scrub(call.arguments),
            result=output,
            error=error,
        )
        if ok and not bypass:
            stored = result.model_dump()
            for field in ("cache_hit", "coalesced", "duration_ms"):
                stored.pop(field, None)
            self.cache.set(key, stored)
        return result


def _build_state() -> dict[str, Any]:
    broadcaster = NotificationBroadcaster(
        cooldown_seconds=_env_float("ALERT_COOLDOWN_SECONDS", 10.0)
    )
    telemetry = LatencyMonitor(
        broadcaster=broadcaster,
        alert_threshold_seconds=_env_float("ALERT_THRESHOLD_SECONDS", 2.0),
    )
    cache = GatewayLRUCache(
        max_size=_env_int("CACHE_MAX_SIZE", 10_000),
        default_ttl_seconds=_env_float("CACHE_TTL_SECONDS", 30.0),
    )
    orchestrator = ToolOrchestrator(
        cache,
        telemetry,
        internal_permits=_env_int("INTERNAL_PERMITS", 15),
        external_permits=_env_int("EXTERNAL_PERMITS", 5),
    )
    return {
        "broadcaster": broadcaster,
        "telemetry": telemetry,
        "cache": cache,
        "orchestrator": orchestrator,
        "ready": False,
        "started": time.time(),
    }


def create_app() -> FastAPI:
    state = _build_state()
    production = os.getenv("GATEWAY_ENV", "development") == "production"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.ready = True
        state["ready"] = True
        try:
            yield
        finally:
            app.state.ready = False
            state["ready"] = False
            await state["broadcaster"].shutdown()

    app = FastAPI(
        title="Meridian Agent Gateway",
        version="1.0.0",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.gateway = state
    app.state.ready = False

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        if not state["ready"]:
            return JSONResponse({"status": "starting"}, status_code=503)
        return JSONResponse({"status": "ready"})

    @app.get("/metrics")
    async def metrics() -> PlainTextResponse:
        telemetry: LatencyMonitor = state["telemetry"]
        cache: GatewayLRUCache = state["cache"]
        counters = cache.snapshot_counters()
        body = telemetry.render_prometheus(
            cache_entries=counters["entries"],
            cache_hits=counters["hits"],
            cache_misses=counters["misses"],
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    @app.get("/gateway/alerts/stream")
    async def alerts_stream(request: Request) -> StreamingResponse:
        broadcaster: NotificationBroadcaster = state["broadcaster"]
        queue = broadcaster.subscribe()

        async def events():
            try:
                yield 'event: ready\ndata: {"ok":true}\n\n'
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    yield f"event: alert\ndata: {json.dumps(item, separators=(',', ':'))}\n\n"
            finally:
                broadcaster.unsubscribe(queue)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post("/agent/execute-tools", response_model=ExecuteResponse)
    async def execute_tools(body: ExecuteRequest) -> ExecuteResponse:
        request_id = uuid.uuid4().hex
        started = time.perf_counter()
        orchestrator: ToolOrchestrator = state["orchestrator"]
        results = await orchestrator.execute(body.tools)
        wall_ms = round((time.perf_counter() - started) * 1000, 3)
        return ExecuteResponse(request_id=request_id, results=results, wall_ms=wall_ms)

    return app


app = create_app()
