# System Architecture

This document is the structural map of the system: the C4-style context and container views, the three runtime sequences that everything else hangs off (call setup, one conversation turn, in-call context delta), the design principles that decided the shape, and the repository layout that mirrors it. It defines *where things run and how they talk*; the behavior inside each box belongs to the owning doc — the voice pipeline to [docs/06](06-voice-pipeline.md), the screen-context transform to [docs/07](07-ui-semantic-context.md), tools to [docs/10](10-tool-calling.md).

**Read this with:** [docs/01](01-product-and-use-case.md) for the product this serves, [docs/06](06-voice-pipeline.md) for the latency budget the turn sequence must hit, [docs/07](07-ui-semantic-context.md) for the context payloads on the data channel, and [docs/15](15-scalability-and-reliability.md) for how the containers are actually run.

---

## 1. System context (C4 Level 1)

One user, one app, one self-hosted platform, three metered external providers. Everything the project *owns* sits inside the platform boundary; everything metered by the minute or the token sits outside it, behind a provider interface.

```mermaid
flowchart LR
    M["Rajesh (merchant)"] -->|"speaks, taps, listens"| A["VyaparPay Android app"]
    A -->|"WebRTC audio + ctx data channel; REST"| P["Voice-agent platform (self-hosted)"]
    subgraph EXT["External providers (metered)"]
        DG["Deepgram (Nova-3 streaming STT)"]
        EL["ElevenLabs (Flash v2.5 TTS)"]
        OR["OpenRouter (Sonnet 5 dialogue, Haiku 4.5 utility)"]
    end
    P -->|"WebSocket, audio in / partials out"| DG
    P -->|"streaming HTTP, text in / audio out"| EL
    P -->|"streaming completions + fallback array"| OR
```

Three boundary facts worth stating once, here, because every later doc assumes them:

1. **The app never talks to a provider.** No Deepgram key, no ElevenLabs key, no OpenRouter key ever ships in the APK. All provider traffic originates server-side, so keys rotate in one place and per-call cost is attributable in one place (`CostTracker`).
2. **The platform is one trust boundary.** Screen content and user speech cross into it as *untrusted input* — the injection defenses in [docs/14](14-security.md) apply at this line, not deeper.
3. **Providers are replaceable by config.** Each sits behind an interface in `backend/app/providers/` (`SttProvider`, `TtsProvider`, `LLMProvider`, `EmbeddingProvider`). Model IDs are env-config (`OPENROUTER_DIALOGUE_MODEL`), not constants. The rejected alternative — calling vendor SDKs directly from pipeline code — saves a week and costs every future migration; ADR discussion in [docs/16](16-tech-stack.md).

---

## 2. Container view (C4 Level 2)

```mermaid
flowchart TB
    subgraph AND["VyaparPay Android app"]
        FEAT["feature modules: dashboard, payments, support"]
        CTX["context capture: core:screencontext + core:analytics"]
        VOICE["voice module: VoiceCallService, WebRtcClient"]
    end
    subgraph PLAT["Voice-agent platform — docker compose, one host in the demo"]
        LK["LiveKit SFU (WebRTC server)"]
        API["agent-api (FastAPI): sessions, seeded business APIs, context ingestion"]
        W["voice-worker (LiveKit Agents, Python): the agent brain"]
        PG[("Postgres 16 + pgvector")]
        RD[("Redis 7")]
        OBS["Grafana + Tempo (OTel traces)"]
    end
    subgraph EXT["External providers"]
        DG["Deepgram"]
        EL["ElevenLabs"]
        OR["OpenRouter"]
    end
    VOICE -->|"channel 1: WebRTC audio + data channel (topic ctx)"| LK
    FEAT -->|"channel 2: REST — POST /v1/sessions"| API
    CTX --> VOICE
    CTX --> FEAT
    LK <-->|"room audio + data messages"| W
    W -->|"STT / TTS / LLM streams"| DG
    W --> EL
    W --> OR
    W -->|"tool calls (HTTP, business APIs)"| API
    W -->|"session state, ctx cache"| RD
    W -->|"memory reads"| PG
    API -->|"seeded fixtures, profiles, summaries, vectors"| PG
    API --> RD
    W -->|"spans"| OBS
    API --> OBS
```

### The two channels from the app

The app maintains exactly two independent northbound paths. Keeping them separate is deliberate: the REST channel exists before and after the call; the WebRTC channel exists only during it.

| | Channel 1: LiveKit (WebRTC) | Channel 2: agent-api (REST) |
|---|---|---|
| Carries | Bidirectional Opus audio; `ctx.delta` / `ctx.event` / `ctx.snapshot` messages on the reliable+ordered data channel, topic `ctx` | `POST /v1/sessions` (initial ScreenContext snapshot + last ~15 events ride in the body); session teardown; non-call app traffic |
| Lifetime | Call only — opened on `CallStateMachine → Connecting`, closed at `Ended` | Always available |
| Auth | Room-scoped LiveKit token, 5-min TTL, minted by agent-api | Demo JWT ([docs/14](14-security.md)) |
| Ordering | Client-monotonic `seq` per envelope; backend detects gaps and requests a full snapshot | Request/response |

**Rejected alternative — a third channel.** An early sketch had a dedicated WebSocket for context, on the theory that context delivery should not depend on the call. That is backwards: context deltas are *only* useful during a call, the LiveKit data channel is already reliable and ordered, and a second socket means a second reconnect state machine on flaky Indian mobile networks. Locked as ADR-4 in [docs/16](16-tech-stack.md); the initial snapshot rides on `POST /v1/sessions` precisely so the data channel never has to exist before the room does.

### Container responsibilities

| Container | Owns | Deliberately does not own |
|---|---|---|
| LiveKit SFU | WebRTC negotiation, Opus forwarding, data-channel delivery, reconnection | Any intelligence — it never sees text, context, or tools |
| agent-api (FastAPI) | Session mint (`POST /v1/sessions` → `{session_id, livekit_url, livekit_token}`), seeded business APIs the tools call, initial-snapshot ingestion, post-call persistence | Audio; it never joins a room |
| voice-worker (LiveKit Agents) | Joins the room as participant "Asha"; runs VAD/STT/LLM/TTS orchestration in `app/voice/`; runs *our* code — `ConversationManager`, `ContextBuilder`, `PromptBuilder`, `ToolExecutor`, `SafetyLayer` — everywhere else | Business data authority — tool calls go to agent-api, not straight to tables |
| Postgres 16 + pgvector | Seeded business fixtures, user profiles, conversation summaries, KB embeddings (1536-dim), per-call cost rows | Hot per-turn state |
| Redis 7 | `session:{id}` (transcript window, tool results, state), `ctx:{session_id}` (latest ScreenContext), `rate:{user_id}` | Anything durable |
| Grafana + Tempo | One OTel trace per turn: `turn` → `stt.final`, `context.build`, `llm.ttft`, `llm.total`, `tool.exec.<name>`, `tts.first_byte` | Alerting/SLOs — explicitly deferred |

Both backend services import the same `app/` package; they differ in entrypoint, not codebase. This is a demo-honest simplification: one repo, one Docker image built twice with different commands. The production split (separate images, independent scaling) is a build-pipeline change, not a code change — see §6.

Android module structure is coarse here by intent (`:app`, `:core:*`, `:feature:*`, `:voice` — full graph in [docs/03](03-android-architecture.md)): the architectural point is that `core:screencontext` and `core:analytics` observe the app, and `:voice` transports what they observe.

### Where the named components run

The doc set names components precisely (naming freeze in the canon); this table places each on a container so later docs never have to re-explain topology. "Both" means the shared `app/` package is imported by both services, but the listed service is where the component actually executes.

| Component | Executes in | When |
|---|---|---|
| `SessionManager` | agent-api | Session mint and teardown |
| `SnapshotIngestor` | agent-api (initial REST snapshot) and voice-worker (data-channel deltas) | Call setup; every delta |
| `ConversationManager`, `ContextBuilder`, `PromptBuilder`, `LLMRouter` | voice-worker | Every turn |
| `ToolExecutor`, `SafetyLayer` | voice-worker (calls agent-api business APIs over HTTP) | Turns with tool calls |
| `EventLog`, `ContextCompressor` | voice-worker | As `ctx.event` messages arrive; on budget pressure |
| `Summarizer` (Haiku), `CostTracker` finalization | voice-worker | Every 6 turns; post-call |
| Memory modules (`SessionMemory`, `SemanticMemory`, ...) | Both read; voice-worker writes during call, post-call persistence via agent-api | Per [docs/09](09-memory-architecture.md) |
| `UiTreeCollector`, `SemanticSnapshotBuilder`, `ScreenContextPublisher` | Android (`core:screencontext`) | Continuously while a call is live |
| `VoiceCallService`, `WebRtcClient`, `CallStateMachine` | Android (`:voice`) | Call lifetime |

The one placement worth defending: **tool execution lives in the worker, but tool *authority* lives in agent-api.** `ToolExecutor` in the worker decides *when* to call a tool; the business API in agent-api decides *whether the session's user may* and owns the data. Executing tools directly against Postgres from the worker would be faster (one hop fewer) and was rejected: it would smear authorization across two services and make the [docs/14](14-security.md) invariant — "tool execution is allowlisted + server-side authorized per session user" — unenforceable at a single point.

---

## 3. Runtime views

Three sequences carry the whole system. Everything else — memory, RAG, safety — plugs into one of these three.

### 3.1 Call setup

The contract: from Rajesh's tap to Asha's first word is ~2 s, and the agent is context-complete *before* audio flows — the greeting needs zero tool calls ([docs/01 §7](01-product-and-use-case.md)).

```mermaid
sequenceDiagram
    participant App as VyaparPay app
    participant API as agent-api
    participant RD as Redis
    participant LK as LiveKit SFU
    participant W as voice-worker
    App->>API: POST /v1/sessions {user_id, screen_context, recent_events}
    Note over App,API: screen_context = retained PaymentScreen snapshot (support screens excluded from capture)
    API->>API: validate snapshot (screen_context/v1), authorize user
    API->>RD: write ctx:{session_id}, create session:{id}
    API-->>App: {session_id, livekit_url, livekit_token} — token 5-min TTL, room-scoped
    par speculative prefetch (server)
        API->>API: profile (Postgres), KB top-3 for DAILY_LIMIT_EXCEEDED (pgvector), warm prompt prefix
    and room join (client)
        App->>LK: connect — publish mic audio, open data channel (topic ctx)
    end
    LK->>W: room active — dispatch worker
    W->>LK: join as participant "Asha", subscribe audio + data
    W->>RD: read ctx:{session_id} + prefetched context
    W-->>App: first TTS audio — the canonical opening line
```

Two structural decisions visible here:

- **The snapshot rides on session creation, not the data channel.** The agent must know the screen before the room exists; putting the snapshot in the REST body removes an entire class of "agent joined before context arrived" races.
- **Prefetch and room join run in parallel.** Connection setup takes long enough (ICE, DTLS, worker dispatch) to hide the profile fetch, the pgvector query, and prompt-prefix warming entirely. The greeting's perceived latency is the *connection* time, not a model round-trip.

### 3.2 One conversation turn

The turn is the unit of everything: one OTel trace, one latency budget, one prompt build. Stage names below are the canonical span names; the millisecond budget per stage (p50 ≤ 1.0 s, p95 ≤ 2.0 s end-to-end) is owned by [docs/06](06-voice-pipeline.md) and not restated here.

```mermaid
sequenceDiagram
    participant R as Rajesh
    participant LK as LiveKit SFU
    participant W as voice-worker
    participant DG as Deepgram
    participant OR as OpenRouter
    participant API as agent-api
    participant EL as ElevenLabs
    R->>LK: speech (Opus frames)
    LK->>W: subscribed audio
    W->>DG: streamed audio (partials flowing back throughout)
    Note over W: VAD endpoint detection (Silero + turn detector)
    DG-->>W: final transcript — span stt.final
    W->>W: ContextBuilder + PromptBuilder — span context.build (Redis reads: session, ctx, summary)
    W->>OR: streaming completion (Sonnet 5, fallback array)
    OR-->>W: first token — span llm.ttft
    opt tool call requested by the model
        W->>API: ToolExecutor → e.g. get_wallet_balance — span tool.exec.get_wallet_balance
        API-->>W: typed result (Pydantic)
        W->>OR: continue with tool result
    end
    W->>EL: first complete sentence dispatched to TTS
    EL-->>W: first audio bytes — span tts.first_byte
    W->>LK: Opus frames (spans close: llm.total, turn)
    LK->>R: Asha's reply begins while the LLM is still generating
```

The load-bearing property is that **no stage waits for the previous stage to finish** — STT emits partials during speech, the LLM streams tokens, TTS receives sentence one while sentence two is still being generated, and audio plays while TTS is still synthesizing. A blocking pipeline with these same providers would stack the full latencies serially and roughly double the turn time; the streamed overlap is what makes the [docs/06](06-voice-pipeline.md) budget reachable at all. Barge-in (Rajesh interrupts mid-reply) cancels the TTS stream and the in-flight LLM generation — mechanics in [docs/06](06-voice-pipeline.md).

### 3.3 Screen-context delta during the call

Rajesh navigates mid-call — the agent's picture of the screen must follow. Deltas flow over the data channel; the full envelope and IR schemas are owned by [docs/07](07-ui-semantic-context.md) and [docs/13](13-api-contracts.md).

```mermaid
sequenceDiagram
    participant UI as Compose UI
    participant Pub as ScreenContextPublisher
    participant LK as LiveKit SFU
    participant W as voice-worker
    participant RD as Redis
    UI->>Pub: semantics change (navigation, dialog, field edit)
    Pub->>Pub: debounce + diff against last snapshot
    Pub->>LK: data channel, topic ctx — {"v":1, "type":"ctx.delta", "seq":42, "ts":..., "payload":{...}}
    LK->>W: reliable, ordered delivery
    W->>W: SnapshotIngestor — validate, check seq continuity
    alt seq continuous
        W->>RD: merge delta into ctx:{session_id}
    else gap detected (seq skipped)
        W->>LK: request full snapshot
        LK->>Pub: forwarded request
        Pub->>LK: {"type":"ctx.snapshot", "seq":43, ...}
        LK->>W: full snapshot replaces ctx:{session_id}
    end
    Note over W,RD: next turn's ContextBuilder reads the updated ctx — no push into a mid-flight LLM call
```

Deltas update *state*, not conversation: an arriving delta never interrupts a generating turn. The agent's knowledge is "the screen as of the moment the turn started," which is the honest semantic — injecting UI changes into a half-generated sentence produces incoherent replies and untraceable prompts. `ctx.event` messages (taps, API errors) flow the same path into `EventLog`. The client-monotonic `seq` exists because reliable+ordered delivery guarantees ordering *per connection*, not across LiveKit reconnects — the gap request is the recovery path for the [docs/01 §7](01-product-and-use-case.md) reconnection failure mode.

### 3.4 What breaks where (architecture-level failure modes)

Per-stage pipeline failures belong to [docs/06](06-voice-pipeline.md) and tool failures to [docs/10](10-tool-calling.md); these are the *container-level* failures — a whole box in §2 going away.

| Failure | Detection | Impact | Mitigation | Degradation |
|---|---|---|---|---|
| LiveKit SFU unreachable at setup | Client connect timeout; `CallStateMachine` stuck in `Connecting` | No call possible | Bounded retry with backoff (token TTL 5 min leaves room) | App falls back to `HelpScreen` complaint flow |
| voice-worker process dies mid-call | LiveKit participant-left event for "Asha"; room has no agent | Asha goes silent | Worker pool re-dispatch; new worker rehydrates from Redis `session:{id}` (transcript window, summary, pending-confirmation state) | The in-flight turn is lost; the conversation is not |
| agent-api down during a call | `tool.exec.<name>` span errors; health check | Tools fail; *new* calls cannot mint sessions | In-call audio is unaffected — worker↔SFU↔providers do not route through agent-api by design | Agent answers from screen context, offers `escalate_to_human` |
| Redis down | Connection errors inside `context.build` | Session memory and ctx cache unreachable | Turn proceeds on the in-process short-term working set | Context-degraded turns; ctx deltas dropped until recovery, then full-snapshot request |
| OpenRouter primary model outage | Completion error/timeout | Turn would fail | Fallback array in the request routes to the configured fallback model — no code path change | Marginally different reply style for the affected turns |
| Deepgram stream drops | WebSocket close mid-utterance | One utterance lost | Reconnect the STT stream; audio keeps flowing via SFU | Asha asks Rajesh to repeat — once, honestly |

The pattern across all six rows: the WebRTC path (app ↔ SFU ↔ worker) and the REST/data path (app ↔ agent-api ↔ stores) fail independently, which is the payoff of the two-channel topology in §2 — a database problem never drops the call audio, and an SFU problem never corrupts session state.

---

## 4. Design principles

Four principles decided nearly every box and arrow above. They are stated here once; ADRs in [docs/16](16-tech-stack.md) carry the full trade-offs.

**Streaming-first.** Every hop that can stream, streams: STT partials, LLM tokens, sentence-level TTS dispatch, chunked audio out. This is not an optimization pass applied later — interfaces were designed as streams from day one, because retrofitting streaming onto request/response interfaces means rewriting them. The cost is real: streaming code paths are harder to test and cancellation (barge-in) must be threaded through every stage. We pay it because a voice agent that responds in 3+ seconds is a demo nobody finishes watching.

**Provider abstraction.** Deepgram, ElevenLabs, and OpenRouter are current defaults, not commitments. `providers/` defines one interface per capability with exactly one implementation each today (`DeepgramStt`, `ElevenLabsTts`, `OpenRouterLLM`, `OpenAIEmbeddings`) — YAGNI says don't build the second implementation, but the *seam* costs one file and buys provider-swap-by-config. OpenRouter itself is a second abstraction layer for LLMs specifically: the fallback array in the request handles model outages without any code on our side.

**Framework at the edges.** LiveKit Agents runs VAD, turn detection, and barge-in mechanics — problems with years of tuning in them that we should not re-solve. But it is confined to `app/voice/`: **the framework moves audio; we own the intelligence.** Context assembly, memory, prompting, tool execution, safety, and routing are framework-agnostic code that would survive replacing LiveKit Agents entirely. The rejected alternative — building *on* a full agent framework's abstractions for memory and tools — was rejected because the signature capability (screen-aware context) is exactly the part no framework ships, and framework-shaped code makes the custom part harder, not easier.

**Contracts in `protocol/`.** Every payload that crosses the Android/backend boundary — the data-channel envelope, `screen_context/v1`, `app_event/v1`, the session REST shapes — is defined once in [protocol/](../protocol/) and consumed by both sides. Neither the Kotlin serializers nor the Pydantic models are authoritative; the schema files are. A contract change is therefore a visible, reviewable diff in one directory, not a drift between two codebases discovered at runtime.

---

## 5. Repository structure

The repo is a monorepo mirroring the container diagram: one directory per deployable surface, plus the shared contract directory both depend on.

```text
voice-calling-agent/
├── android/                  # VyaparPay app (Kotlin, Jetpack Compose)
│   ├── app/                  #   :app — wiring, DI, navigation host
│   ├── core/                 #   :core:ui, :core:network, :core:analytics, :core:screencontext
│   ├── feature/              #   :feature:dashboard, :feature:payments, :feature:support
│   └── voice/                #   :voice — VoiceCallService, WebRtcClient, CallStateMachine
├── backend/                  # Python services (shared app/ package, two entrypoints)
│   ├── app/
│   │   ├── agent/            #   ConversationManager, PromptBuilder, ToolExecutor, SafetyLayer, ...
│   │   ├── context/          #   SnapshotIngestor, EventLog, ContextCompressor
│   │   ├── memory/           #   short-term / session / profile / semantic (pgvector)
│   │   ├── tools/            #   registry + one module per tool (contracts in docs/10)
│   │   ├── providers/        #   LLMProvider, SttProvider, TtsProvider, EmbeddingProvider
│   │   ├── voice/            #   VoiceAgentWorker — the LiveKit Agents adapter
│   │   └── models/           #   Pydantic + SQLAlchemy
│   ├── seeds/                #   fixture data + reset script (the staged business)
│   └── tests/
├── protocol/                 # shared contracts: JSON Schemas + canonical examples
│   ├── screen_context/       #   screen_context/v1
│   ├── events/               #   app_event/v1
│   ├── channel/              #   data-channel envelope (topic ctx)
│   └── rest/                 #   /v1/sessions request/response shapes
├── infra/                    # docker-compose.yml, livekit.yaml, otel-collector, grafana dashboards
├── docs/                     # this doc set (01–17)
└── .github/                  # CI: android build, backend lint+test, schema validation
```

Rationale per top-level directory:

| Directory | Why it exists as a top-level unit |
|---|---|
| `android/` | One deployable (the APK), one toolchain (Gradle), one reviewer mental model; its internal module graph is [docs/03](03-android-architecture.md)'s concern |
| `backend/` | Both services share `app/` deliberately — `ConversationManager` and `SnapshotIngestor` are used by voice-worker and agent-api; splitting into two packages now would force a shared-library third package for zero benefit at this scale |
| `protocol/` | The load-bearing directory: language-neutral schemas both `android/` and `backend/` consume. CI validates the canonical examples against the schemas and both codebases' serializers against the examples, so a breaking change fails the build on *both* sides in the same PR |
| `infra/` | Everything needed to run the platform is declarative and versioned next to the code it runs; `docker compose up` is the entire local setup ([docs/15](15-scalability-and-reliability.md)) |
| `docs/` | The doc set is a first-class deliverable of this portfolio project, not an afterthought; it is reviewed like code |
| `.github/` | CI is split per surface (Android build, backend tests, schema validation) so an Android-only PR never waits on a Python test matrix |

**Why a monorepo at all:** the alternative — separate app and backend repos — was rejected because the project's hardest correctness problem is the cross-boundary contract, and a monorepo makes every contract change atomic: schema, Kotlin consumer, Python consumer, and docs move in one reviewable commit. Multi-repo coordination overhead buys nothing for a single-team (single-person) project.

**On `protocol/` specifically**, since it is the one non-obvious directory: it is the source-of-truth answer to "who owns the wire format?" Without it, the Pydantic models would quietly become authoritative (backend-defined contracts) and the Android side would chase them. With it, `screen_context/v1` is a schema file plus the canonical Rajesh-incident example from [docs/07](07-ui-semantic-context.md); the Kotlin `SemanticSnapshotBuilder` output and the Python `SnapshotIngestor` input are both *tested against the same fixture*. Versioned schema names (`/v1`) make evolution explicit: v2 is a new file and a migration note, never an in-place edit.

---

## 6. Deployment views

### Local (demo — what this repo actually ships)

One machine, one command. `infra/docker-compose.yml` starts seven containers; the Android debug APK is side-loaded and pointed at the machine's LAN IP.

| Container | Image basis | Notes |
|---|---|---|
| `livekit` | LiveKit OSS server | UDP ports for WebRTC; dev config in `infra/livekit.yaml` |
| `agent-api` | `backend/` image, uvicorn entrypoint | Serves sessions + seeded business APIs |
| `voice-worker` | same `backend/` image, worker entrypoint | Registers with LiveKit for room dispatch |
| `postgres` | postgres:16 + pgvector | Seeded on first start from `backend/seeds/` |
| `redis` | redis:7 | No persistence configured — session state is disposable in the demo |
| `tempo` + `grafana` | Grafana stack | Pre-provisioned dashboard: per-turn traces, token counts, cost |

Demo-honest caveats: single host means the SFU, the worker, and the databases share CPU — turn latency figures measured locally exclude real internet RTT to providers only partially (provider calls do leave the machine); there is no TLS between containers; secrets are a local `.env` from `.env.example`.

### Production evolution (sketch — full treatment in [docs/15](15-scalability-and-reliability.md))

The container boundaries are already the scaling boundaries; production is a topology change, not an architecture change.

| Concern | Demo | Production evolution |
|---|---|---|
| SFU | One LiveKit container | Multi-node LiveKit or LiveKit Cloud; TURN infrastructure for restrictive networks |
| voice-worker | One process | Horizontally scaled pool; LiveKit dispatch load-balances rooms across workers — one call pins one worker process, so scaling is call-count-linear |
| agent-api | One uvicorn | Stateless replicas behind a load balancer; separate image from the worker |
| Postgres | One container | Managed Postgres + read replicas; pgvector holds until ~10M vectors (flip condition, ADR-3) |
| Redis | One container | Managed Redis with persistence; Redis Streams → Kafka only at event-bus scale (ADR-3 flip condition) |
| Secrets | `.env` file | Secret manager, per-service scoping, key rotation |
| Observability | Compose Tempo/Grafana | Managed tracing + the deferred items: alerting, SLOs, eval platform |

The single riskiest production delta is the worker pool: a voice call is a long-lived stateful process (minutes), not a stateless request, so deploys need connection draining and capacity planning is concurrent-calls-based, not RPS-based. [docs/15](15-scalability-and-reliability.md) owns that analysis.

---

## Decisions this document exports

| Fixed here | Value | Heaviest consumer |
|---|---|---|
| Two-channel client topology | WebRTC audio + `ctx` data channel to LiveKit; REST to agent-api; no third channel | [docs/03](03-android-architecture.md), [docs/13](13-api-contracts.md) |
| Snapshot-on-session-create | Initial ScreenContext rides `POST /v1/sessions`, deltas ride the data channel | [docs/07](07-ui-semantic-context.md) |
| Shared `app/` package, two entrypoints | One backend image, two commands | [docs/04](04-backend-architecture.md), [docs/15](15-scalability-and-reliability.md) |
| Turn-boundary context semantics | Deltas update Redis state; never injected mid-generation | [docs/08](08-context-and-events.md), [docs/11](11-prompt-engineering.md) |
| `protocol/` as contract authority | Schemas + fixtures, CI-enforced on both sides | Every cross-boundary doc |
| Repo layout | §5 tree, verbatim | Contributing, CI, [docs/17](17-roadmap.md) |
