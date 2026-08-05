# backend/ — Agent Backend (Python / FastAPI)

Phase 2 of the VyaparPay voice agent: the full text-only intelligence loop —
context → prompt → LLM → tools → safety → cost — resolving the canonical
Rajesh/₹245/`DAILY_LIMIT_EXCEEDED` incident end to end against seeded
Postgres data, with real tool calls and a confirm-gated mutation. No
WebRTC, no STT/TTS, no screen context — those are Phase 3+
([docs/17-roadmap.md](../docs/17-roadmap.md)). See
[docs/04-backend-architecture.md](../docs/04-backend-architecture.md) and
[docs/05-agent-architecture.md](../docs/05-agent-architecture.md) for the
architecture this implements.

For a guided walkthrough of the demo, see [DEMO.md](DEMO.md).

## Package map (as built)

```
app/
├── main.py         # ASGI entrypoint (Dockerfile CMD: uvicorn app.main:app) —
│                   #   thin re-export shim over app/api/main.py's create_app()
├── api/            # FastAPI app factory, middleware, REST routes
│   ├── main.py     #   create_app() + lifespan (builds every process singleton)
│   ├── middleware.py   # RequestId -> Tracing -> Auth -> ErrorEnvelope
│   ├── errors.py   #   AppError hierarchy + error/success envelope (shared with tools/)
│   ├── deps.py     #   get_db, require_rate_limit, JWT verification
│   └── routes/     #   health, wallet, payments, limits
├── agent/          # The agent brain (docs/05 §3) — one module per component
│   ├── session_manager.py     # SessionManager — lifecycle + post-call drain
│   ├── context_builder.py     # ContextBuilder — assembles the ContextBundle
│   ├── prompt_builder.py      # PromptBuilder — renders the 9-slot prompt
│   ├── llm_router.py          # LLMRouter — tier routing, streamed tool-call reassembly
│   ├── tool_executor.py       # ToolExecutor — validate/authorize/gate/idempotency/audit
│   ├── safety_layer.py        # SafetyLayer — input fence, output screen, affirmation
│   ├── cost_tracker.py        # CostTracker — per-turn cost, budget guard, call_costs
│   ├── conversation_manager.py# ConversationManager — the per-turn orchestrator
│   └── prompts/                # persona.md, business_rules.md (doc-verbatim prompt text)
├── tools/          # @tool registry + one module per business tool
│   ├── registry.py             # @tool decorator, allowlist, tool-invocation bridge
│   ├── errors.py                # validation/business/timeout error-shape builders
│   ├── get_wallet_balance.py
│   ├── get_payment_status.py
│   └── request_limit_increase.py
├── memory/         # ShortTermMemory (in-process), SessionMemory (Redis-backed)
├── providers/      # OpenRouterLLM — the LLMProvider implementation
├── data/           # Engine/sessionmaker factory, RedisClient, repository-per-aggregate
│   └── repositories/   # Merchant/Wallet/Payment/Limit/Conversation/ToolAudit/Cost
├── domain/         # Frozen contract: value types (types.py) + Protocols (interfaces.py)
├── models/         # SQLAlchemy ORM (8 Phase-2 tables)
├── obs/            # structlog + OpenTelemetry wiring
└── config.py       # Settings (pydantic-settings, fail-fast on missing secrets)
scripts/
├── seed.py         # Idempotent demo fixture seeder (Rajesh/Kumar General Store/...)
├── demo_cli.py     # Text REPL harness — stands in for the Phase-3 voice transport
└── smoke_providers/ # Live Deepgram/ElevenLabs smoke harness (operator-run, real APIs — see below)
tests/
├── agent/, api/, data/, memory/, models/, obs/, providers/, scripts/, tools/  # unit tests
└── e2e/            # test_canonical_conversation.py — the full 9-turn replay
```

Everything above is real, merged code — this map replaced Phase 1's
"planned package map" placeholder once Phase 2 landed.

## Quickstart

The compose file lives at the **repo root**, one level up from `backend/`
— everything else below assumes `backend/` as the working directory
(`alembic.ini`, `pyproject.toml`'s `scripts` package, and `.env` all live
there). Two working directories, two steps:

```bash
# from the repo root
docker compose up -d postgres redis
```

```bash
# from backend/
cp .env.example .env          # fill in OPENROUTER_API_KEY at minimum
alembic upgrade head
python -m scripts.seed
```

Then either run the test suite or the interactive demo (see
[DEMO.md](DEMO.md)).

## Development

All commands below assume `backend/` as the working directory.

```bash
pip install -e ".[dev]"

pytest tests --ignore=tests/models --ignore=tests/e2e  # no Docker required
pytest tests/models tests/e2e  # testcontainers-gated: need Docker (Postgres)
pytest tests/e2e/test_voice_pipeline_e2e.py  # no Docker; needs `pip install -e ".[dev,voice]"`
ruff check .
mypy app
```

Two test *files* are testcontainers-gated (`tests/models/test_orm.py`,
`tests/e2e/test_canonical_conversation.py`) and need a running Docker
daemon — everything else runs against hand-rolled fakes
(`tests/fakes.py`'s `FakeLLM`, `tests/support/fake_redis.py`'s
`FakeRedis`) with no external services.

`tests/e2e/test_voice_pipeline_e2e.py` is the exception inside a gated
*directory*: it needs no container, only the `[voice]` extra, so it gets
its own line above. The first command's blanket `--ignore=tests/e2e` skips
it, and without the extra it `importorskip`s to a silent green skip — which
is exactly how it went unrun for a whole phase (CI runs it explicitly in the
`gates` job now; see `.github/workflows/ci.yml`).

## Configuration

All settings are environment variables read by `app/config.py`
(`Settings`, pydantic-settings) — see [.env.example](.env.example) for
the full list with comments. Three fields have no default and the
process fails fast at startup if they're unset: `JWT_SECRET`,
`DATABASE_URL`, `OPENROUTER_API_KEY`.

## Live-provider smoke harness

`app/providers/deepgram.py` and `app/providers/elevenlabs.py` are wire-
tested only against in-process fake WebSocket servers
(`tests/providers/`) — no live API keys exist in CI or the default dev
environment, so every vendor-pinned behavior those modules document
(`PINNED(vendor)` in the tests, the numbered judgment calls in the
provider docstrings) has never been checked against the real endpoints.
`scripts/smoke_providers/` is the operator-run harness that closes that
gap: it talks to the real Deepgram and ElevenLabs APIs and prints a
PASS/FAIL/UNKNOWN verdict per assumption, cross-referenced to the
judgment-call number it validates.

**This is not a test.** It is not in `tests/`, it is never run by CI or
`pytest`, and running it costs real (small) vendor usage charges. Needs
`DEEPGRAM_API_KEY` and/or `ELEVENLABS_API_KEY` + `ELEVENLABS_VOICE_ID`
set (`.env.example` has the fields; the harness refuses to run and names
whichever env var is missing otherwise) and the `[voice]` extra
installed (`pip install -e ".[dev,voice]"`).

```bash
python -m scripts.smoke_providers                    # both legs, zero-asset synthetic audio
python -m scripts.smoke_providers --wav sample.wav    # both legs, real speech (needed for the
                                                       # transcript-dependent Deepgram checks)
python -m scripts.smoke_providers --deepgram-only
python -m scripts.smoke_providers --elevenlabs-only
python -m scripts.smoke_providers --help              # full flag list, incl. --skip-keepalive
```

It prints the estimated usage before making any request, never prints or
logs an API key, and exits non-zero only if a pinned vendor assumption
was actively contradicted (`UNKNOWN` — "the run never exercised this" —
never fails the run; only `FAIL` does).
