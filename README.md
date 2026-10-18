# Meridian — LLM Agent API Gateway

A multi-worker FastAPI gateway that sits between a reasoning model and downstream tools. It runs internal and external calls concurrently, caps each lane so a burst cannot stampede the backends, scrubs PII before anything is cached or alerted, and folds latency spikes into one notification instead of a flood.

The in-browser console (when this service is shipped inside the Meridian workspace) speaks the same contract: `POST /agent/execute-tools`, `GET /gateway/alerts/stream`, `GET /metrics`, `GET /healthz`, `GET /readyz`.

## What it enforces

| Control | Behavior |
| --- | --- |
| Lanes | `internal_*` tools share 15 permits. Everything else shares 5. |
| Single-flight | Identical in-flight calls (after redaction) share one execution. |
| Cache | Thread-safe LRU, 10,000 entries, 30s TTL. Keys are SHA-256 of scrubbed inputs. |
| Redaction | Fuzzy sensitive keys, bearer / JWT / vendor-key regex, SSN, Luhn card numbers. `subject`, `message`, and `description` pass through. |
| Alerts | Tool time over 2.0s emits immediately, then groups the next 10s into one bulleted Slack/Discord payload. |
| Edge | Nginx terminates TLS, rate-limits the API, and does not buffer the alert stream. |

`internal_clock` is not cached.

## Layout

```text
fastapi-ai-gateway/
├── .github/workflows/deploy.yml
├── app/
│   ├── main.py
│   ├── cache.py
│   ├── telemetry.py
│   ├── notifications.py
│   └── security.py
├── nginx/fastapi-gateway.conf
├── monitoring/
│   ├── prometheus.yml
│   ├── alert.rules.yml
│   └── grafana/          # datasource + dashboard provisioning
├── scripts/stress_test.py
├── tests/
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## Secrets

Do not commit a `.env`. Export these in the shell or in the secret store your orchestrator already uses:

```bash
export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export GRAFANA_ADMIN_PASSWORD="replace-me"
```

Empty webhook URLs are safe: alerts still hit the SSE stream and Prometheus, and outbound posts are skipped. The compose file defaults the Grafana password to `admin_secure_pass_2026` only when the variable is unset — override it before any shared environment.

Optional tuning: `ALERT_THRESHOLD_SECONDS` (2.0), `ALERT_COOLDOWN_SECONDS` (10), `CACHE_MAX_SIZE` (10000), `CACHE_TTL_SECONDS` (30), `INTERNAL_PERMITS` (15), `EXTERNAL_PERMITS` (5).

## Run the stack

From this directory:

```bash
docker compose up -d --build
docker compose ps
```

| Surface | URL | Credentials |
| --- | --- | --- |
| Gateway, via Nginx | `http://localhost` and `https://localhost` | none (TLS is a generated local certificate until you mount a real one) |
| Prometheus | `http://localhost:9090` | none |
| Grafana | `http://localhost:3000` | user `admin`, password from `GRAFANA_ADMIN_PASSWORD` |

Prometheus scrapes `api_gateway:8000/metrics` every 5 seconds. Grafana loads the Meridian dashboard from `monitoring/grafana`. Alert rules in `monitoring/alert.rules.yml` are evaluated by Prometheus; delivery to chat is the application's webhook path, not a separate Alertmanager process.

Mount your own certificate over the `gateway_certs` volume (`tls.crt`, `tls.key`) for anything beyond a local check. The proxy generates a 30-day self-signed certificate only when those files are missing.

The image runs Gunicorn with two Uvicorn workers as uid 10001. Cache and SSE subscribers are per worker — put a shared cache in front if you scale workers and need one keyspace. Docs (`/docs`) are off when `GATEWAY_ENV=production`.

## Call the gateway

```bash
curl -s http://localhost/healthz
curl -s http://localhost/readyz
curl -s http://localhost/metrics

curl -s http://localhost/agent/execute-tools \
  -H 'content-type: application/json' \
  -d '{
    "tools": [
      {"name": "internal_lookup", "arguments": {"sku": "MX-204", "description": "bin check"}},
      {"name": "external_fx", "arguments": {"pair": "USDINR", "api_key": "sk-live-abcdefghijklmnop"}},
      {"name": "internal_lookup", "arguments": {"sku": "MX-204", "description": "bin check"}}
    ]
  }'
```

The third call reuses the first. `api_key` is `[REDACTED]` on the response and is not part of the cache key material. `description` is kept.

Built-in adapters (no network egress): `internal_lookup`, `internal_sum` (`values` array), `internal_clock`, `external_fx`, `external_search`. Any other well-formed name is accepted and echoed after redaction. `delay_ms` (capped at 8000) stalls that call so you can trip the 2s alert without a real upstream.

Alert stream:

```bash
curl -N http://localhost/gateway/alerts/stream
```

## Tests and load

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -q
python -m compileall -q app scripts

pip install httpx
python scripts/stress_test.py
```

What you should see:

1. **Cache.** Wave 1 pays the tool latency. Wave 2 repeats `internal_lookup` and the reported tool time stays under 1 ms (`cache_hit: true`).
2. **Debounce.** The two slow `external_fx` calls cross 2s. The stream (and Slack/Discord, if configured) gets one immediate spike, then a single bulleted summary about 10 seconds later.
3. **Metrics.** `gateway_average_latency_seconds` and `gateway_calls_total` move. Grafana's Meridian dashboard reads the same series.

## CI

`.github/workflows/deploy.yml` compiles, runs the unit tests, and builds the image on every pull request and on `main`. A registry push runs only on `main` when `REGISTRY_HOST`, `REGISTRY_USER`, and `REGISTRY_PASSWORD` are set.

## Operating notes

- Request bodies are capped at 32 KiB of arguments and 32 tools. Nginx rejects bodies over 256 KB and rate-limits at 30 requests/second with a burst of 60. The alert stream is excluded from that limit.
- Logs record tool name and failure class, not argument payloads.
- Webhook posts time out in 3 seconds and never fail the tool call.
- `/healthz` is process liveness. `/readyz` is 503 until the lifespan has started and again during shutdown.
