# Backend Architecture

This document owns the shape of the Python backend: the two entrypoints that share one `app/` package, the package layout and what each module is responsible for, how configuration, dependency injection, data access, and rate limiting are wired, and — the section this doc *owns* for the whole set — the observability stack: structlog processors, OpenTelemetry traces, the Grafana dashboard, and Tempo trace-to-log correlation. It is the "how the server is built" companion to [docs/05](05-agent-architecture.md), which owns the agent brain's behavior; this doc owns the runtime the brain executes inside. Where a wire contract is involved, [docs/13](13-api-contracts.md) is authoritative and this doc references it.

**Read this with:** [docs/05](05-agent-architecture.md) for the fourteen brain modules whose runtime this document describes, [docs/13 §5](13-api-contracts.md) for the voice-worker ↔ agent-api seam summarized here, [docs/16](16-tech-stack.md) for the ADRs (async Python, Postgres+Redis, observability stack) this doc implements, and [docs/09](09-memory-architecture.md) for the data layer the repositories front.

---

## 1. Process model: two entrypoints, one codebase

The demo ships **one Docker image, built once, started two ways**. Both entrypoints import the same [backend/app/](../backend/app/) package; they differ in `CMD`, not in code ([docs/02 §6](02-system-architecture.md)).

```mermaid
flowchart TB
    subgraph IMG["one backend image (backend/)"]
        APP["app/ — shared package"]
        subgraph E1["entrypoint 1"]
            API["agent-api: uvicorn app.main:app"]
        end
        subgraph E2["entrypoint 2"]
            W["voice-worker: python -m app.voice.run"]
        end
        API --> APP
        W --> APP
    end
    APP --> PG[("Postgres 16 + pgvector")]
    APP --> RD[("Redis 7")]
    W -. "tool calls over HTTP (localhost)" .-> API
    AND["Android peer (libwebrtc)"] <-. "WSS /v1/signal + DTLS-SRTP media + ctx data channel" .-> W
    AND -. "TURN relay when P2P fails" .-> CT["coturn (STUN + TURN)"]
    CT -.-> W
```

| Entrypoint | Command | Owns | Never does |
|---|---|---|---|
| **agent-api** | `uvicorn app.main:app` | Session mint (signaling token + TURN credentials), seeded business APIs, initial-context ingestion, post-call persistence | Terminates a WebRTC peer or touches audio |
| **voice-worker** | `python -m app.voice.run` (asyncio service) | Hosts the `/v1/signal` WebSocket, owns one aiortc `RTCPeerConnection` per call, runs the per-call `VoiceAgentWorker` and the per-turn brain, calls agent-api's business APIs over HTTP for tool data | Owns business data directly — it reads through the API, never straight from tables |

**Why one deployable for the demo.** Two images, service discovery, and mTLS between them is production plumbing for a system that runs on one `docker compose up`. Splitting now would force a third shared-library package for `ConversationManager` and `SnapshotIngestor`, which both entrypoints use, at zero benefit at demo scale (rejected in [docs/02 §7](02-system-architecture.md)). The seam is kept honest anyway: even in the demo, tool handlers reach business data over HTTP to `localhost` (~1–2 ms), never by importing the business modules. That ~2 ms is the cheapest insurance in the repo — the production split becomes a build-pipeline change (build twice, repoint the worker's base URL, swap the shared-secret JWT for exchanged tokens), not a refactor ([docs/13 §5](13-api-contracts.md)).

**Async-first, end to end.** Every I/O path is `asyncio`: FastAPI on `uvicorn`, SQLAlchemy 2.0 async with `asyncpg`, `redis.asyncio`, and `httpx.AsyncClient` for every provider (OpenRouter, Deepgram, ElevenLabs, OpenAI embeddings). The discipline is load-bearing on the worker: one blocking call in the event loop stalls *every* concurrent call on that process ([docs/16 §4](16-tech-stack.md)). The structural mitigations — no sync HTTP client anywhere, no sync DB driver, and the per-frame CPU work the hand-rolled pipeline does own (Opus codec via PyAV, Silero inference via onnxruntime) confined to native code that releases the GIL, with anything heavier pushed to a thread — are enforced in review, and the per-turn spans (§7) turn an accidental stall into an anomalous `context.build` duration instead of a mystery hang.

---

## 2. Package layout

The tree, matching [backend/README.md](../backend/README.md) and the canon naming freeze (§3). One responsibility line each; the deep-dive doc owns behavior.

```
backend/
├── app/
│   ├── main.py          # FastAPI app factory + lifespan (singleton wiring, §4)
│   ├── config.py        # pydantic-settings Settings; fail-fast on missing secrets (§3)
│   ├── api/             # FastAPI routers: sessions, context ingestion, seeded business APIs
│   │   └── deps.py      #   request-scope dependencies: db session, principal, rate limiter
│   ├── agent/           # the brain (docs/05): SessionManager, ConversationManager,
│   │                    #   PromptBuilder, ContextBuilder, ToolExecutor, LLMRouter,
│   │                    #   SafetyLayer, CostTracker, Summarizer
│   ├── context/         # SnapshotIngestor, EventLog, ContextCompressor (docs/08)
│   ├── memory/          # ShortTermMemory, SessionMemory, UserProfileMemory,
│   │                    #   SemanticMemory (pgvector), ConversationSummaryStore (docs/09)
│   ├── tools/           # tool registry + one module per tool (16 tools, docs/10)
│   ├── providers/       # LLMProvider→OpenRouterLLM, SttProvider→DeepgramStt,
│   │                    #   TtsProvider→ElevenLabsTts, EmbeddingProvider→OpenAIEmbeddings
│   ├── voice/           # hand-rolled WebRTC + voice pipeline (docs/06): SignalingServer,
│   │                    #   PeerSession, AudioIngress, VadEndpointer, AudioEgress,
│   │                    #   VoiceAgentWorker
│   ├── data/            # repositories, SQLAlchemy engine/session, Redis client (§5)
│   ├── obs/             # structlog config, OTel setup, PII redactor (§7)
│   └── models/          # Pydantic schemas + SQLAlchemy ORM (docs/12)
├── migrations/          # Alembic versions
├── tests/               # unit / contract / integration / e2e (§8)
└── scripts/             # seed.py, dev helpers (§9)
```

The boundary that matters most is `app/voice/`: per [docs/05 §1.1](05-agent-architecture.md) it is the only package allowed to import `aiortc`, `av`, or `onnxruntime`. Everything else speaks in `ContextBundle`, `Message`, `ToolResult`, `TurnCost` — which is why every module below it is testable without a peer connection (§8).

**Audio dependencies.** `aiortc` pulls in PyAV (ffmpeg bindings) for Opus encode/decode and resampling, and `onnxruntime` (CPU) runs the Silero VAD model — all confined to `app/voice/`. Because both entrypoints share one image (§1), agent-api carries ffmpeg it never uses; the extra image weight is accepted for the single-image simplicity, and a production split would trim it out of the api image as a side effect.

---

## 3. Configuration

`app/config.py` is a single `pydantic-settings` `BaseSettings`. Env is the only source (canon §12 — secrets via env, nothing hardcoded); a missing required secret raises at import time, so the process dies at startup rather than at the first call ([docs/16](16-tech-stack.md)). `.env.example` documents every key and is the file a reviewer copies to `.env`.

| Env var | Purpose | Example / default |
|---|---|---|
| `ENV` | Runtime profile | `dev` |
| `LOG_LEVEL` | structlog threshold | `INFO` |
| `JWT_SECRET` | HS256 demo-JWT secret ([docs/13 §1.2](13-api-contracts.md)) | *(required)* |
| `DATABASE_URL` | Postgres DSN, async driver | `postgresql+asyncpg://vyapar:...@postgres:5432/vyapar` |
| `REDIS_URL` | Redis 7 connection | `redis://redis:6379/0` |
| `OPENROUTER_API_KEY` | LLM gateway key | *(required)* |
| `OPENROUTER_BASE_URL` | OpenAI-compatible base | `https://openrouter.ai/api/v1` |
| `OPENROUTER_DIALOGUE_MODEL` | Current dialogue default | `anthropic/claude-sonnet-5` |
| `OPENROUTER_UTILITY_MODEL` | Current utility default | `anthropic/claude-haiku-4-5` |
| `OPENROUTER_DIALOGUE_FALLBACKS` | Ordered fallback slugs (comma) | `openai/gpt-...,google/gemini-...` |
| `DEEPGRAM_API_KEY` / `DEEPGRAM_MODEL` | STT provider | key / `nova-3` |
| `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` | TTS provider (Flash v2.5) | key / voice id |
| `OPENAI_API_KEY` / `EMBEDDING_MODEL` | Embeddings ([docs/09](09-memory-architecture.md)) | key / `text-embedding-3-small` |
| `SIGNALING_PUBLIC_URL` | Public `wss` base of the voice-worker `/v1/signal` endpoint, returned in the session response ([docs/13 §2.1](13-api-contracts.md)) | `wss://voice.vyapar.local/v1/signal` |
| `SESSION_TOKEN_SECRET` | HMAC secret for one-time signaling tokens (5-min TTL; minted by agent-api, verified by voice-worker) | *(required)* |
| `TURN_SECRET` | coturn `use-auth-secret` shared secret; agent-api mints 10-min HMAC TURN credentials with it (canon §12) | *(required)* |
| `COTURN_HOST` | Public host stamped into the `turn:`/`turns:` URLs in `ice_servers` | `turn.vyapar.local` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Trace export target | `http://tempo:4317` |
| `OTEL_SERVICE_NAME` | Service label on spans | `agent-api` / `voice-worker` |
| `SESSION_TTL_SECONDS` | `session:{id}` expiry (canon §11) | `86400` |
| `RATE_LIMIT_SESSIONS_PER_MIN` | Session-create window (§6) | `5` |
| `CALL_COST_CAP_USD` | Runaway guard ([docs/05 §3.8](05-agent-architecture.md)) | `1.00` |

Model IDs are **current defaults, not constants** (canon §5): the router reads `settings.dialogue_model` at request time, and the fallback slugs live in `models: [...]`, so a model swap is an env edit and a redeploy, never a code change. The one place this rule visibly pays off is the `.env.example` diff a reviewer reads to understand the whole external surface — the provider keys, the two transport secrets (`SESSION_TOKEN_SECRET`, `TURN_SECRET`), and two model IDs, with nothing hidden in source.

---

## 4. Dependency injection

Two scopes, two mechanisms. **Request/turn scope** uses FastAPI's `Depends`. **Process singletons** (providers, engine, Redis pool) are built once in the app lifespan and handed out by trivial accessors — protocol-typed so tests substitute fakes without touching the container.

```python
# app/main.py
@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    http = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=2.0))  # pooled, shared
    app.state.settings = settings
    app.state.sessionmaker = async_sessionmaker(create_async_engine(settings.database_url))
    app.state.redis = redis.from_url(settings.redis_url, decode_responses=True)
    app.state.llm: LLMProvider = OpenRouterLLM(http, settings)     # singleton, protocol-typed
    app.state.stt: SttProvider = DeepgramStt(http, settings)
    app.state.tts: TtsProvider = ElevenLabsTts(http, settings)
    app.state.embed: EmbeddingProvider = OpenAIEmbeddings(http, settings)
    setup_observability(settings)                                  # §7
    yield
    await http.aclose(); await app.state.redis.aclose()
```

```python
# app/api/deps.py — request scope
async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    async with request.app.state.sessionmaker() as session:       # one txn scope per request
        yield session

def get_llm(request: Request) -> LLMProvider:
    return request.app.state.llm                                  # the startup singleton

async def get_principal(cred: HTTPAuthorizationCredentials = Depends(bearer)) -> SessionUser:
    return verify_demo_jwt(cred.credentials, get_settings().jwt_secret)   # docs/13 §1.2
```

### 4.1 The agent-api middleware stack

Request-scope cross-cutting concerns are ASGI middleware, applied outermost-first, so every route inherits them without decoration. Order is deliberate:

| # | Middleware | Responsibility |
|---|---|---|
| 1 | `RequestIdMiddleware` | Mint/propagate `request_id`, bind it into `contextvars` for structlog (§7.1) |
| 2 | `TracingMiddleware` | Open the request span (OTel), so downstream logs carry `trace_id` |
| 3 | `AuthMiddleware` | Verify the demo JWT once, attach `SessionUser` to the request ([docs/13 §1.2](13-api-contracts.md)) |
| 4 | `ErrorEnvelopeMiddleware` | Map any raised `AppError`/`RateLimited`/validation error to the `{success,data,error,meta}` envelope + status code ([docs/13 §1](13-api-contracts.md)); stack traces go to structlog, never to the client |

The rate limiter (§6) is a route dependency, not middleware, because it applies to a named subset (session create + mutating endpoints), not every route — and it must run *after* auth so the window is keyed on the verified `sub`, not a spoofable body field. Business-data reads from the worker cross this same stack over HTTP carrying a **service JWT** (`sub` = the session user, `act = "svc_voice-worker"`): the business endpoint authorizes the *merchant* while the audit logs the *actor* ([docs/13 §5](13-api-contracts.md)).

Every provider is defined as a `Protocol` and injected by that type, never by concrete class. The `LLMProvider` protocol plus the `OpenRouterLLM` sketch — showing the **OpenAI-compatible `chat.completions` call, the `models` fallback array, and the usage extraction `CostTracker` consumes** ([docs/05 §3.8](05-agent-architecture.md)):

```python
# app/providers/base.py
class LLMProvider(Protocol):
    async def stream(
        self, messages: list[dict], *, models: list[str],
        tools: list[dict] | None = None,
    ) -> AsyncIterator[LLMEvent]: ...

# app/providers/openrouter.py
class OpenRouterLLM:                                    # implements LLMProvider
    def __init__(self, http: httpx.AsyncClient, settings: Settings):
        self._http = http
        self._url = f"{settings.openrouter_base_url}/chat/completions"
        self._headers = {"Authorization": f"Bearer {settings.openrouter_api_key}"}

    async def stream(self, messages, *, models, tools=None):
        body = {
            "models": models,          # e.g. [dialogue_model, fallback_1, fallback_2]
            "messages": messages,
            "tools": tools,
            "stream": True,
            "usage": {"include": True},   # gateway streams a final usage frame
        }
        async with self._http.stream("POST", self._url, headers=self._headers, json=body) as r:
            r.raise_for_status()
            async for line in r.aiter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                chunk = orjson.loads(line[6:])
                if chunk.get("usage"):                 # → CostTracker.record_turn(...)
                    yield UsageEvent(model=chunk["model"], usage=chunk["usage"])
                else:
                    yield TokenEvent(delta=chunk["choices"][0]["delta"])
```

Three things this sketch fixes for the rest of the system. The `models` array means a primary-model 5xx re-routes **at the provider edge** inside the same HTTP call ([docs/05 §3.4](05-agent-architecture.md)); the `chunk["model"]` on the usage frame is what the router logs as a span attribute so a silent fallback shows up in the trace, not as a quality dip; and the `usage` frame (input, output, cached-prefix token counts) is the single source `CostTracker` multiplies by per-token price for both the span attributes and the `call_costs` row (§7, [docs/16 §5](16-tech-stack.md)). The `SttProvider`, `TtsProvider`, and `EmbeddingProvider` protocols follow the identical shape — one file per vendor, config-flipped — which is what makes "swap the TTS vendor" a bounded change ([docs/05 §5](05-agent-architecture.md)).

---

## 5. Data access

**Repository-per-aggregate.** Business logic depends on a narrow repository interface, not on SQLAlchemy. One repository per aggregate root, each owning its tables and its invariants; cross-aggregate reads are explicit joins inside a repository, never leaked ORM relationships walked by callers.

| Repository | Aggregate / tables ([docs/12](12-data-models.md)) | Primary reader |
|---|---|---|
| `ConversationRepo` | `conversations`, `conversation_summaries` | `SessionManager`, post-call pipeline |
| `WalletRepo` | `wallet_accounts`, `cards` | `get_wallet_balance`, `block_card` |
| `PaymentRepo` | `transactions`, `merchant_limits` | `get_payment_status`, `retry_payment` |
| `LimitRepo` | `merchant_limits` (limit-increase folded in, [docs/12 §3.2](12-data-models.md)) | `request_limit_increase` |
| `OrderRepo` / `ComplaintRepo` | `device_orders`, `complaints` | order/complaint tools |
| `ProfileRepo` | `user_profiles` | `ContextBuilder`, post-call profile write |
| `SemanticRepo` | `kb_articles`, `memory_chunks` (pgvector) | `SemanticMemory` retriever |
| `ToolAuditRepo` | `tool_invocations` | `ToolExecutor` audit ([docs/10 §5](10-tool-calling.md)) |
| `CostRepo` | `call_costs` | `CostTracker.finalize` |

```python
class Repository(Protocol[T]):
    async def get(self, id: str) -> T | None: ...
    async def add(self, entity: T) -> T: ...
    async def update(self, entity: T) -> T: ...
```

**Transaction boundaries.** One `AsyncSession` = one transaction, opened by `get_db` per request and committed on clean exit / rolled back on exception. The post-call pipeline is the boundary that matters: the conversation summary and its pgvector embedding write in **one** transaction ([docs/16 ADR-003](16-tech-stack.md)) so a summary can never exist without its vector or vice versa. Mutating tool handlers commit their business write and the `tool_invocations` audit row together — a business mutation without an audit row is exactly the state the tool layer is built to prevent ([docs/10 §5](10-tool-calling.md)).

**Migrations are code from day one.** Alembic autogenerates against the ORM models; `alembic upgrade head` runs at container start before uvicorn binds, and the seed script (§9) runs after. No "add the column by hand" step exists — the schema in [docs/12](12-data-models.md) is reproducible from `migrations/` on an empty database.

**Redis access module.** `app/data/redis.py` wraps the one async client pool and exposes typed helpers keyed by the canon keyspace (canon §11) — `session:{id}` (hash: transcript window, tool digests, state, running `cost_usd`; TTL `SESSION_TTL_SECONDS`), `ctx:{session_id}` (current IR + event ring, 60-min TTL), and `rate:{user_id}` (§6). Callers never assemble key strings; the helper owns the prefix, which keeps a typo from silently reading the wrong namespace and keeps every key greppable.

**Money units.** REST speaks integer paise (`*_paise`), the tool boundary speaks integer rupees, and the conversion happens once inside each tool handler ([docs/13 §1](13-api-contracts.md)); no float rupee amount exists anywhere, so repositories store and return paise and never do arithmetic that could round.

---

## 6. Rate limiting

A Redis **sliding-window** counter per user, keyed `rate:{user_id}` (canon §11). It guards the two surfaces that can be abused into cost: session creation (the expensive one — it mints signaling and TURN credentials, prefetches context, and warms the LLM cache) and the mutating business endpoints. Session creation is capped at **5 per minute** per user (`RATE_LIMIT_SESSIONS_PER_MIN`, [docs/13 §1.1](13-api-contracts.md)); exceeding it returns `429 RATE_LIMITED` with a `Retry-After` header, and the app shows "couldn't start the call" with no session minted and nothing to clean up.

```python
# app/api/deps.py — sorted-set sliding window (atomic via pipeline)
async def enforce_rate(redis, user_id: str, *, limit: int, window_s: int = 60) -> None:
    key, now = f"rate:{user_id}", time.time()
    async with redis.pipeline(transaction=True) as p:
        p.zremrangebyscore(key, 0, now - window_s)    # evict entries outside the window
        p.zadd(key, {f"{now}:{uuid4().hex}": now})    # record this hit
        p.zcard(key)                                  # count in-window
        p.expire(key, window_s)
        _, _, count, _ = await p.execute()
    if count > limit:
        raise RateLimited(retry_after=window_s)
```

A true sliding window (sorted set of timestamps) is used over a fixed-window counter because a fixed window lets a caller fire `2 × limit` across a window boundary; the ZSET evicts by score every call, so the limit holds continuously. It costs one extra `zremrangebyscore` per check — trivial at demo scale, correct at any scale. **Demo vs production:** the window is per-user in one Redis; production would add a per-IP window in front of auth, a global concurrency cap on `voice-worker` dispatch, and WAF-level limits — but the same `rate:{user_id}` primitive carries over unchanged.

---

## 7. Observability

This section is authoritative for the doc set (canon §4, ADR-005 in [docs/16](16-tech-stack.md)). A voice agent makes two falsifiable claims — latency and cost — and neither is honest without per-turn instrumentation. The whole stack ships in compose: **structlog JSON logs → Loki, OpenTelemetry traces → Tempo, both viewed in Grafana.** No SaaS, no vendor lock; the 90-second demo ends on a Grafana trace and a cost row.

### 7.1 structlog processor chain

Logs are structured JSON, one object per line, with `session_id` / `turn_id` / `request_id` bound in `contextvars` so every line inside a turn carries them without being passed around. `request_id` is set by an ASGI middleware in agent-api; `session_id` and `turn_id` are bound by `ConversationManager` when it opens a turn. The processor order is load-bearing:

```python
# app/obs/logging.py
structlog.configure(processors=[
    structlog.contextvars.merge_contextvars,      # 1. pull session_id/turn_id/request_id
    structlog.processors.add_log_level,           # 2.
    structlog.processors.TimeStamper("iso", utc=True),
    add_otel_ids,                                 # 3. inject trace_id/span_id from active span
    redact_pii,                                   # 4. mask card / Aadhaar / PAN (defense in depth)
    structlog.processors.format_exc_info,         # 5.
    structlog.processors.JSONRenderer(orjson.dumps),  # 6. final line
])
```

`redact_pii` is the **last-line** defense, not the primary one: `SafetyLayer` already masks card/Aadhaar/PAN patterns before anything reaches TTS or a log call (canon §12, [docs/05 §3.10](05-agent-architecture.md)). The processor re-scans every rendered event anyway, because a log statement added in six months might carry a raw field the author forgot to mask — belt and suspenders, and it costs one regex pass per line. `add_otel_ids` is what makes trace-to-log correlation work (§7.4): it stamps the active span's `trace_id`/`span_id` onto the log object, so a log line and its span are joinable both directions.

The event catalog logged into each turn's span is fixed by [docs/05 §3.10](05-agent-architecture.md) — `turn_started`, `stt_final`, `llm_first_token`, `tool_executed`, `tts_first_byte`, `turn_completed`, `call_ended` — and the dashboard below reads exactly those fields. Because PII is masked before the log call, the JSON stream is safe to ship to Loki without a second redaction pass.

### 7.2 OpenTelemetry: one trace per turn

The OTel tracer is configured in `setup_observability` with an OTLP exporter to `OTEL_EXPORTER_OTLP_ENDPOINT` (Tempo), a batching span processor (so export never blocks a turn), and instrumentation for the ASGI app and `httpx`:

```python
# app/obs/tracing.py
def setup_observability(settings: Settings) -> None:
    provider = TracerProvider(resource=Resource.create(
        {"service.name": settings.otel_service_name}))
    provider.add_span_processor(BatchSpanProcessor(       # buffers; drops on overflow, never blocks
        OTLPSpanExporter(endpoint=settings.otel_exporter_otlp_endpoint)))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()                # provider calls appear as child spans
```

**One trace per conversation turn** (canon §4). The root span is `turn`; its children carry the stage latencies the [docs/06](06-voice-pipeline.md) budget decomposes. Span names and attributes are frozen by canon:

| Span | Opened by | Attributes |
|---|---|---|
| `turn` (root) | `ConversationManager.on_stt_final` | `session_id`, `turn_no`, `interrupted`, `turn_ms` |
| `stt.final` | `VoiceAgentWorker` | `stt_ms`, `text_len`, `is_endpoint` |
| `context.build` | `ContextBuilder` | `ctx_ms`, `slots_filled`, `degraded` |
| `llm.ttft` | `LLMRouter` | `ttft_ms`, `model`, `tier`, `cache_hit` |
| `llm.total` | `LLMRouter` | `llm_ms`, `input_tokens`, `output_tokens`, `cost_usd` |
| `tool.exec.<name>` | `ToolExecutor` | `tier`, `status`, `latency_ms`, `idempotency_key` |
| `tts.first_byte` | `TtsProvider` | `tts_ttfb_ms`, `sentence_no` |

The `llm.cost_usd` span attribute and the `cost_usd` field on the `turn_completed` log line are the **same number seen two ways** ([docs/05 §3.10](05-agent-architecture.md)) — the trace answers "where did the second go", the logs answer "what happened", and they agree because both come from the one provider `usage` frame (§4).

### 7.3 Grafana dashboard spec

Six panels. Time-series latency comes from Tempo TraceQL metrics over span durations; cost, tokens, and error counts come from LogQL over the structlog JSON in Loki (those fields live on log events, not just spans). Each panel with its source-query sketch:

| Panel | Source | Query sketch |
|---|---|---|
| **Turn latency p50/p95 by stage** (stacked) | Tempo TraceQL metrics | `{name =~ "stt.final\|context.build\|llm.total\|tool.exec.*\|tts.first_byte"} \| quantile_over_time(duration, .95) by (name)` — one series per stage, stacked to the ~1.9 s p95 total |
| **Cost per call** | Loki LogQL | `avg_over_time({service="voice-worker"} \| json \| event="call_ended" \| unwrap call_cost_usd [$__interval])` — overlaid against the ≈ $0.30 canon line |
| **Tool latency by name** | Tempo TraceQL metrics | `{name =~ "tool.exec.*"} \| quantile_over_time(duration, .95) by (name)` — surfaces the 2 s tool ceiling per tool |
| **Error rate** | Loki LogQL | `sum(rate({service=~"agent-api\|voice-worker"} \| json \| level="error" [5m])) / sum(rate({...}[5m]))` — split by `error.code` |
| **Active calls** | Loki LogQL (gauge) | `count_over_time({...} \| json \| event="turn_started"[1m]) - count_over_time({...} \| event="call_ended"[1m])` — running in-flight sessions (Redis `session:*` count is the cross-check) |
| **Tokens per turn** (in / out) | Loki LogQL | `avg_over_time({...} \| json \| event="turn_completed" \| unwrap input_tokens [$__interval])` and same for `output_tokens` — watched against the ≤ 2,500 in / ≤ 150 out budget ([docs/11](11-prompt-engineering.md)) |

The dashboard is designed to be *read against the budgets*, not in the abstract: the latency panel's stacked total should sit under the p95 line from canon §7, the cost panel against ≈ $0.30 from [docs/16 §5](16-tech-stack.md), the tokens panel against the [docs/11](11-prompt-engineering.md) slot budget. A panel that drifts off its reference line is the signal — which is why every panel has a canonical number to drift *from*.

### 7.4 Tempo trace-to-log correlation

Grafana's Tempo data source defines a **trace-to-logs** link on `trace_id`; the Loki data source defines a **derived field** that extracts `trace_id` from each JSON log line. Because `add_otel_ids` (§7.1) stamps `trace_id`/`span_id` onto every structlog event, the jump works both ways: click a slow `llm.total` span in a `turn` trace and land on that turn's `llm_first_token` / `turn_completed` log lines; click a log line's trace link and land back on the full turn waterfall. This is the whole debugging loop for a latency regression — find the anomalous stage in the trace, read the structured logs for that exact turn, no `grep` across hosts.

### 7.5 Observability failure modes

Doc-set convention: Failure | Detection | Impact | Mitigation | Degradation.

| Failure | Detection | Impact | Mitigation | Degradation |
|---|---|---|---|---|
| Tempo/OTLP exporter down | Exporter queue errors in agent-api logs | Traces stop landing; latency panels go blank | Batch span processor buffers and drops on overflow — never blocks the turn | Logs (Loki) still carry `*_ms` fields; latency observable, just not as a waterfall |
| Loki down | Grafana log panels empty | Cost/tokens/error panels blank | structlog still writes to stdout; compose captures container logs | Traces still carry `cost_usd`/token span attrs as the fallback source |
| structlog line drop (best-effort) | Missing sequence in `turn_no` on a panel | One dashboard data point lost | Logging is off the critical path by design ([docs/05 §3.10](05-agent-architecture.md)) | A dropped line never drops a turn — the call is unaffected |
| Redis down (rate limiter) | `enforce_rate` raises on connect | Rate checks fail | **Fail closed** on session-create (return `503 SESSION_CAPACITY`) so a Redis outage can't uncap cost | New calls rejected; in-flight calls, whose state is already in Redis, drop with the store |

---

## 8. Testing

Coverage target **80%** (repo rule), enforced in CI. The package boundary (§2) is what makes the target reachable: the brain is exercised without a peer connection, audio, or the WebRTC stack.

| Layer | Scope | Tooling |
|---|---|---|
| **Unit** | Each `app/agent`, `app/context`, `app/memory` module against fake providers | `pytest`, `pytest-asyncio`, fakes implementing the provider `Protocol`s |
| **Contract** | Every Pydantic model round-tripped against the [protocol/](../protocol/) JSON Schemas + fixtures | `pytest` + `jsonschema`; fails on any drift ([docs/13 §7](13-api-contracts.md)) |
| **Integration** | Repositories + API routes against real Postgres/Redis | `testcontainers` (postgres:16 w/ pgvector, redis:7), Alembic `upgrade head` in fixture |
| **Fake-peer** | `SignalingServer` + `PeerSession` + audio path against a real second peer | In-process **aiortc client peer** over a loopback WS, driving PCM fixtures |
| **Provider mocking** | OpenRouter/Deepgram/ElevenLabs HTTP stubbed at the wire | `respx` over `httpx` — asserts the request bodies (fallback array, `usage.include`) |
| **E2E** | One scripted conversation over **text**, bypassing audio | Drives `ConversationManager` with `stt.final` strings; asserts sentences, tool calls, spans |

**Unit — fake providers.** `FakeLLM` yields a scripted `TokenEvent`/`UsageEvent` stream; `FakeTts` records sentences; `FakeEmbeddings` returns fixed vectors. A test feeds a `stt.final` string and asserts on emitted sentences, `tool_call`s, the confirm gate, and span attributes — no network. The canonical call's turns are fixtures here.

**Contract tests** are the seam that keeps Python and Kotlin honest: Pydantic model exports are diffed against `screen_context/v1`, `app_event/v1`, the data-channel envelope, and the tool schemas in [protocol/](../protocol/); if a model and a schema disagree, the build fails and neither side wins silently ([docs/13 §7](13-api-contracts.md)).

**Integration** uses `testcontainers` so tests run against a real Postgres 16 with the pgvector extension and a real Redis 7 — the transaction boundaries (§5), the sliding-window ZSET (§6), and the HNSW top-3 retrieval ([docs/09](09-memory-architecture.md)) are all things a mock would fake wrongly.

**Fake-peer integration** is the transport's own test, made possible by aiortc being a library rather than a hosted service: the test spins up an in-process aiortc *client* peer that dials the `SignalingServer` over a local WebSocket, completes the real offer/answer + trickle-ICE handshake, opens the `ctx` data channel, and streams canned PCM fixtures (the canonical utterances) through actual Opus encode. Assertions cover `VadEndpointer` endpoint timing against the fixtures' known silence gaps, the `transcript.*` and `agent.state` frames coming back on the data channel, and clean teardown on `bye` — the whole WebRTC surface exercised in one process, no phone, no network ([docs/06](06-voice-pipeline.md)).

**E2E over text** is the highest-value cheap test: it runs the full brain — context build, prompt, LLM (via `respx`-stubbed OpenRouter), tool loop, safety checks, cost — end to end for the canonical `DAILY_LIMIT_EXCEEDED` scenario, and asserts the resolution path (`request_limit_increase` → confirm → summary) and that no voiced number is absent from a tool result ([docs/05 §3.6](05-agent-architecture.md)). Audio, VAD, and barge-in timing are out of scope here and covered in the voice-pipeline tests ([docs/06](06-voice-pipeline.md)).

---

## 9. Local dev workflow

The whole stack is one compose file; the loop from clone to a live curl'd session is five commands.

```bash
# 1. bring up the stack — postgres(+pgvector), redis, coturn, tempo, loki, grafana,
#    agent-api, voice-worker. Migrations run on agent-api start.
cp backend/.env.example backend/.env          # fill provider keys
docker compose up -d

# 2. seed fixtures — Rajesh Kumar, Kumar General Store, ₹18,450 wallet, the
#    declined ₹245 txn, KB articles + their embeddings. Prints demo JWTs.
docker compose exec agent-api python -m scripts.seed

# 3. the voice-worker is already running as its own compose service; tail it
docker compose logs -f voice-worker

# 4. mint a session over REST (context rides in the body, docs/13 §2.1)
curl -s http://localhost:8000/v1/sessions \
  -H "Authorization: Bearer $RAJESH_JWT" -H 'Content-Type: application/json' \
  -d @scripts/fixtures/session_create.json | jq
#   → { session_id, signaling_url, signaling_token, ice_servers, expires }
```

The seed script is the demo's backbone: it writes the exact canonical fixtures (canon §2) so every worked example in the doc set is reproducible against a fresh database, and it prints one long-lived JWT per merchant since there is no login flow ([docs/13 §1.2](13-api-contracts.md)). The `session_create.json` fixture is byte-identical to the request in [docs/13 §2.1](13-api-contracts.md), so the curl above reproduces the canonical incident.

**Device testing on a real phone.** The Android app needs three reachable surfaces: agent-api (REST), the voice-worker `/v1/signal` WebSocket, and coturn. `ngrok http 8000` fronts agent-api, and a second `ngrok http` tunnel (or a LAN IP) fronts the signaling WS — `SIGNALING_PUBLIC_URL` is set to whatever the phone can reach. Media does not tunnel: the phone reaches coturn directly on the host's LAN IP (compose maps coturn's UDP/TCP listening ports), with `COTURN_HOST` pointed at that IP so the minted `ice_servers` resolve. This is a **demo-only** convenience — production terminates TLS at a real gateway with coturn on a public IP and real DNS, not an ngrok tunnel — and it is called out as such so nobody mistakes the tunnel for architecture.

---

## Decisions this document exports

| Fixed here | Value | Heaviest consumer |
|---|---|---|
| Process model | One image, two entrypoints (uvicorn agent-api / asyncio voice-worker hosting `/v1/signal` + aiortc peers); tool data over HTTP even in demo | [docs/02](02-system-architecture.md), [docs/13](13-api-contracts.md) |
| Async-first stack | asyncio + SQLAlchemy 2 async/asyncpg + `httpx.AsyncClient`; no sync I/O on any path | [docs/16](16-tech-stack.md) |
| Provider DI | `Protocol`-typed singletons in lifespan; request scope via `Depends`; fakes in tests | [docs/05](05-agent-architecture.md), [docs/06](06-voice-pipeline.md) |
| OpenRouter call shape | `chat.completions` stream, `models:[...]` fallback array, `usage` frame → `CostTracker` | [docs/05](05-agent-architecture.md), [docs/16](16-tech-stack.md) |
| Data access | Repository-per-aggregate; Alembic from day one; summary+embedding in one txn; typed Redis keyspace | [docs/09](09-memory-architecture.md), [docs/12](12-data-models.md) |
| Rate limiting | Redis sliding-window ZSET `rate:{user_id}`; 5 session-creates/min; fail-closed on Redis down | [docs/13](13-api-contracts.md) |
| Observability stack | structlog JSON→Loki, OTel one-trace-per-turn→Tempo, Grafana; frozen span names + 7-event catalog | [docs/05](05-agent-architecture.md), [docs/16](16-tech-stack.md) |
| Dashboard spec | 6 panels, each with a source query and a canonical reference line to drift from | [docs/16](16-tech-stack.md) |
| Testing | Unit (fake providers) / contract (protocol/) / integration (testcontainers) / fake-peer (in-process aiortc client) / text e2e; 80% + respx | CI, [docs/13](13-api-contracts.md) |
