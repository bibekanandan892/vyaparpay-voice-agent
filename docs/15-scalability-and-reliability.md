# Scalability & Reliability

This document owns how the system grows from one `docker compose up` to a real merchant base, and how it stays honest when a box, a network leg, or a provider fails underneath a live call. The scaling axis is not requests per second — it is **concurrent calls**, each one a long-lived, stateful, capacity-bound session pinning a worker for minutes ([docs/06 §8](06-voice-pipeline.md)). That single fact reshapes every component's growth path and makes the third-party providers, not our own compute, the real ceiling. This doc is also the **canonical home of the degradation ladder** — the five rungs Asha walks down as context, then voice, then the agent itself become unavailable — which every failure path in the doc set drops onto.

**Read this with:** [docs/06](06-voice-pipeline.md) for the reconnection layers and 30 s session grace this doc scales, [docs/04](04-backend-architecture.md) for the rate limiter, fail-closed policy, and per-turn spans the SLOs read from, [docs/16](16-tech-stack.md) for the ≈ $0.30/call figure and the pgvector-flip ADR this doc extrapolates, and [docs/02](02-system-architecture.md) for the two-channel topology the capacity model grows.

---

## 1. The scaling axis is concurrent calls

A REST API scales on requests per second; a stateful voice agent does not. One call holds a WebRTC PeerConnection, a subscribed audio track, a worker's async task, a `session:{id}` Redis hash, and an open turn loop for its **entire duration** — a 5-minute call is ~15 turns but one continuous resource reservation. Capacity planning is therefore *concurrent live calls*, and the derived rates (session creates/sec, post-call writes/sec) fall out of the call duration.

Two numbers anchor everything below: the **≈ $0.30 (~₹25) per-call** cost ([docs/16 §5](16-tech-stack.md), canon §9 — referenced, never re-derived here) and the **p50 ≤ 1.0 s / p95 ≤ 2.0 s turn budget** ([docs/06 §3](06-voice-pipeline.md), canon §7). Scaling must hold the second flat while the first multiplies by concurrency.

|  | Stateless REST | Voice agent |
|---|---|---|
| Unit of load | Request (ms-lived) | **Call** (minutes-lived, stateful) |
| Scale signal | Requests/sec, CPU | **Concurrent live calls** |
| Scale-in | Free — drain in-flight in ms | **Cordon-and-drain** over a call-duration (§2.3) |
| Cost driver | Compute | **Provider spend** (§3.3) |

Every row on the right is a consequence of one call being a minutes-long resource reservation, and each reshapes a component's growth path below.

---

## 2. Capacity model

Per component, what the demo ships versus what production needs, and the resource that actually bounds each one. The demo runs everything on a single compose host ([docs/04 §1](04-backend-architecture.md)); the production column is the evolution, not what this repo builds.

| Component | Demo (single compose host) | Production evolution | Bound by |
|---|---|---|---|
| **agent-api** | 1 `uvicorn` process | N stateless replicas behind an LB; horizontal-pod autoscale on RPS/CPU | Nothing structural — stateless, trivially horizontal ([docs/04 §1](04-backend-architecture.md)) |
| **voice-worker** | 1 worker sharing the host — effectively ~1 live call ([docs/06 §8](06-voice-pipeline.md)) | Worker pool; **est. 20–40 concurrent calls per 4-vCPU worker**; autoscale on the concurrent-call gauge; workers register with LiveKit for job dispatch | CPU: Silero VAD + turn-detector inference, Opus encode/decode, resampling. Provider I/O is `async`, not the bound |
| **LiveKit SFU** | Single node, co-located with the worker | Multi-node cluster (Redis-coordinated) + regional PoPs, or LiveKit Cloud | Participant/track fan-out and egress bandwidth — 10k calls ≈ 20k audio tracks |
| **Postgres 16 + pgvector** | Single container | Primary + read replicas + **pgbouncer**; pgvector until **~10M vectors**, then a dedicated vector DB (canon §4.3, [docs/09](09-memory-architecture.md)) | Write throughput (post-call batch) and connection count (a big worker pool = many connections) |
| **Redis 7** | Single container | Redis Cluster (sharded + replicas) | Ops/sec and HA — **not** memory; per-session state is small (§5) |
| **Providers** (Deepgram, ElevenLabs, OpenRouter) | Starter tiers, ~1 call | Enterprise / committed-throughput contracts | Provider concurrency + rate ceilings — **the binding constraint (§2.1)** |

The worker line reconciles with [docs/06 §8](06-voice-pipeline.md)'s "one call pins one worker": *pins* is affinity — a call stays on one worker for its lifetime (its in-process turn state and Redis session are worker-local) — while that worker's event loop multiplexes 20–40 such calls before VAD/turn-detector inference and codec work saturate 4 vCPU. The 20–40 figure is an **estimate to be confirmed by the load harness (§8)**, not a measured number; the demo never runs more than one call, so this is exactly the axis the demo cannot exercise and the load test must.

### 2.1 Provider ceilings — the real bottleneck

Our compute scales linearly with money and instances. The providers do not: each enforces a concurrency and/or rate ceiling by plan tier, and those ceilings — not vCPU — are what cap concurrent calls first. The numbers below are **order-of-magnitude; verify against your actual tier**, because they move with plan and contract.

| Provider | What caps concurrency | Order-of-magnitude starter ceiling | To reach high concurrency |
|---|---|---|---|
| **Deepgram Nova-3** (STT) | Concurrent streaming connections per project/tier | Tens of concurrent streams on standard tiers | Enterprise committed concurrency; one open stream per live call |
| **ElevenLabs Flash v2.5** (TTS) | Concurrent-request cap per plan | Single- to low-double-digit concurrent syntheses on starter/creator tiers | The **tightest** standard-tier ceiling — needs enterprise/committed concurrency well before the others bite |
| **OpenRouter → Anthropic** (LLM) | Req/s + credit-based rate; upstream model-provider capacity | Tens of req/s per key | Provisioned/committed throughput on the upstream model behind the gateway |

The consequence is a procurement fact stated as an engineering one: **you will hit ElevenLabs' concurrency cap before you hit your worker CPU ceiling** on any non-enterprise tier. Scaling voice is negotiating provider contracts as much as adding pods, and the per-call cost (§1) is almost entirely provider spend, so the same ceilings that gate throughput also drive the bill (§3.3).

### 2.2 What consumes a worker's 4 vCPU

The 20–40 estimate is not arbitrary — it falls out of what is actually CPU-bound per call. The expensive intelligence runs on the *provider's* hardware; the worker only streams tokens and moves audio.

| Per-call CPU cost | Weight | Notes |
|---|---|---|
| Opus decode (uplink) + encode (downlink) | Moderate, continuous | 20 ms frames both directions for the call's life ([docs/06 §2](06-voice-pipeline.md)) |
| Resampling 48k↔16k and 24k↔48k | Low | Fixed-ratio, sub-ms per chunk ([docs/06 §2.2](06-voice-pipeline.md)) |
| Silero VAD inference | Low, per-frame | Runs continuously on the subscribed track |
| Turn-detector model inference | **Highest single item**, per-endpoint | Small transformer — the semantic completeness gate ([docs/06 §4](06-voice-pipeline.md)) |
| Orchestration / asyncio | Low | Context build, sentence chunking, span emission |
| Provider stream I/O (Deepgram, ElevenLabs, OpenRouter) | **Not CPU-bound** | Async network — this is precisely why one worker holds *many* calls |

The asymmetry — small-model inference and codec on our CPU, the LLM on the provider's GPUs — is why a 4-vCPU worker multiplexes tens of calls rather than one. It is not unbounded, because that work still accumulates linearly, and the async discipline is load-bearing: one accidental blocking call stalls *every* call on the loop ([docs/04 §1](04-backend-architecture.md)). The load harness (§8) is designed to surface that as a latency cliff before it reaches production.

### 2.3 Autoscaling a stateful pool

Scale-**out** is easy: a new worker boots, registers with LiveKit, and starts receiving dispatched jobs — no rebalancing of existing calls. Scale-**in** is the hard direction a stateless service never faces — you cannot terminate a worker holding 30 live calls. The pool scales in by **cordon-and-drain**: mark the worker ineligible for new dispatch, let its calls end naturally (bounded by the ~15 min max-call-duration watchdog, §6), then terminate. Two consequences follow: scale-in lags a load drop by up to one call-duration, so off-peak savings arrive slowly; and because a call is **pinned** (§2), there is no live session migration — rebalancing means draining, not moving. Size the pool for the worst-case scale-in window, not the instantaneous minimum.

---

## 3. Worked scenario: 10,000 concurrent calls

Concrete target: **10,000 simultaneous live calls**, 5-minute average duration. Everything below is order-of-magnitude sizing to show *what changes*, not a deploy spec.

Derived rates (from concurrency ÷ duration): **~33 new sessions/sec** (10,000 ÷ 300 s), ~33 post-call write batches/sec, and ~500 turns/sec aggregate (~15 turns per 5-min call). These are the numbers each tier sizes against.

| Component | What changes at 10k concurrent |
|---|---|
| **voice-worker** | At 30 calls/worker → **~350 workers** (250–500 range, with headroom), ~1,400 vCPU. Autoscaled on the concurrent-call gauge; LiveKit dispatches new jobs across the registered pool. This is the largest compute line and still small money (§3.3) |
| **agent-api** | Stateless, so scale to sustain ~33 session-creates/sec **plus** worker→API tool-read traffic (~500 turns/sec, a fraction hitting business reads). ~8–12 replicas behind the LB; session-create is the expensive path (mints room, prefetch, warms cache — [docs/04 §6](04-backend-architecture.md)) |
| **LiveKit** | Single node → **multi-node SFU cluster**, Redis-coordinated, regionally sited so the audio hop the §3-budget assumes small stays small ([docs/06 §8](06-voice-pipeline.md)). ~10–20 nodes or LiveKit Cloud; 10k calls ≈ 20k tracks to fan out |
| **Postgres** | Primary + **read replicas** (profile reads, pgvector KB retrieval, business tool reads at session-create rate) + **pgbouncer** (transaction pooling — 350 workers would otherwise exhaust connection slots). Writes (~33 batches/sec) stay on the primary |
| **pgvector** | Each call adds one summary embedding. 10k concurrent isn't 10M vectors, but sustained traffic crosses **~10M vectors** in weeks — the canon §4.3 flip point to a dedicated vector DB (Qdrant/Milvus/pgvector-scale) for the retrieval path |
| **Redis** | Single → **Redis Cluster** (sharded + replicas). Sized for ops/sec and HA, not memory — 10k `session:{id}` hashes are small (§5) |
| **Providers** | The binding tier: **~10k concurrent Deepgram streams, ~10k concurrent ElevenLabs syntheses, ~500 LLM turns/sec through OpenRouter** — all enterprise/committed. This is contracted capacity, not an autoscale group |

### 3.1 Deployment shape at 10k

```mermaid
flowchart TB
    subgraph EDGE["edge"]
        LB["API load balancer"]
        SFU["LiveKit SFU cluster (regional PoPs)"]
    end
    subgraph API["agent-api tier (stateless)"]
        A1["agent-api x8-12"]
    end
    subgraph WK["voice-worker pool (~350 x 4-vCPU)"]
        W1["worker 1..N (20-40 calls each)"]
    end
    subgraph DATA["data tier"]
        PGB["pgbouncer"]
        PGP[("Postgres primary (writes)")]
        PGR[("read replicas")]
        VDB[("vector DB (>10M vectors)")]
        RC[("Redis Cluster")]
    end
    subgraph PROV["providers (committed concurrency)"]
        DG["Deepgram"]
        EL["ElevenLabs"]
        OR["OpenRouter to Anthropic"]
    end
    LB --> A1
    A1 --> PGB --> PGP
    PGB --> PGR
    A1 --> RC
    A1 -. "dispatch registration" .-> SFU
    SFU --> W1
    W1 --> DG
    W1 --> EL
    W1 --> OR
    W1 -. "tool reads over HTTP" .-> LB
    W1 --> RC
    A1 --> VDB
```

Note the worker→API tool-read arrow still routes over the LB even at scale — the `localhost` HTTP seam from the demo ([docs/04 §1](04-backend-architecture.md)) becomes a real network hop but the *code* does not change; that ~2 ms insurance is what makes this a topology change, not a refactor.

### 3.2 What does **not** change

The admission-control 503, the per-call watchdogs (§6), the degradation ladder (§4), the reconnection policy and 30 s grace ([docs/06 §6](06-voice-pipeline.md)), and the `rate:{user_id}` sliding window ([docs/04 §6](04-backend-architecture.md)) are all identical from 1 call to 10k. They were designed as per-session or per-user primitives, so concurrency is a deployment number, not a rewrite.

### 3.3 Cost extrapolation

Using the canonical **≈ $0.30 (~₹25) per completed call** ([docs/16 §5](16-tech-stack.md) — not re-derived here):

| Quantity | Value |
|---|---|
| Throughput at 10k concurrent, 5-min calls | 10,000 ÷ 5 min = **~2,000 calls/min = ~120,000 calls/hr** |
| Variable provider + model cost | 120,000 × $0.30 ≈ **$36,000/hr** (STT + LLM + TTS) |
| Worker compute (~350 × 4-vCPU) | **~$100–150/hr** on-demand cloud |
| API + LiveKit + data tiers | a few hundred $/hr combined |

The punchline: **compute is under ~1% of the bill.** At voice-agent scale the cost is provider spend, which is why prompt prefix caching (LLM $0.10 vs $0.16/call, canon §9) and sentence-level TTS dispatch ([docs/06 §3.2](06-voice-pipeline.md)) are the highest-leverage cost levers — a 10% cut on the variable line dwarfs any infra optimization. Sustained 10k concurrent is a **peak** figure; real traffic is diurnal, so autoscaling the worker pool down off-peak is where compute savings actually live.

### 3.4 Regional siting and data residency

VyaparPay is an India-market product (canon §1), which turns "add regions" into hard constraints, not just latency tuning:

| Concern | Constraint at scale |
|---|---|
| SFU siting | Regional PoPs near callers so the §3 audio hop stays small — a Mumbai caller must not relay through Virginia ([docs/06 §8](06-voice-pipeline.md)) |
| Data residency | Merchant PII + transcripts stay in-region (Indian data-localization expectations for fintech); the Postgres primary and vector DB are region-pinned |
| Provider RTT | Region-pinned Deepgram/ElevenLabs/OpenRouter endpoints — the TTFT/TTFB budget lines ([docs/06 §3](06-voice-pipeline.md)) assume low provider RTT a single demo host only partly reflects |
| Cross-region writes | Regional primaries with per-region ownership; a multi-region active-active Postgres is explicitly **not** attempted |

The one thing multi-region does **not** change is the per-call brain: a call is served entirely within one region, so no turn ever crosses a region boundary mid-conversation.

**Replica-lag caveat.** The post-call summary + embedding write to the primary ([docs/04 §5](04-backend-architecture.md)); a follow-up call minutes later runs its pgvector retrieval against a read replica. If replication lags, the just-written summary is briefly not retrievable. This is acceptable by design — semantic memory is best-effort top-3 ([docs/09](09-memory-architecture.md)), and anything that must be immediately consistent (the current call's own state) lives in Redis, never a replica.

---

## 4. The degradation ladder

**This is the canonical definition; every failure path in the doc set terminates on one of these rungs.** The system's governing rule is *degrade the surface, never fake the call* ([docs/06 §7](06-voice-pipeline.md)). Asha walks down the ladder one rung at a time, and each rung is still a working support agent — just with less context or a narrower channel.

```mermaid
flowchart LR
    R1["1. Full context"] --> R2["2. Stale-context flag"]
    R2 --> R3["3. No screen context (profile + tools)"]
    R3 --> R4["4. Text-chat fallback"]
    R4 --> R5["5. Human handoff"]
```

| Rung | What Asha still has | Trigger conditions |
|---|---|---|
| **1. Full context** | Screen-context IR + profile + memory + tools — the signature capability | Normal operation: fresh `ctx:{session_id}`, all providers healthy |
| **2. Stale-context flag** | Same, but the screen snapshot is marked *possibly out of date* — Asha stops asserting live UI state and confirms instead (*"are you still on the payment screen?"*) | Data-channel `seq` gap not recovered ([docs/02 §3.3](02-system-architecture.md)); snapshot age exceeds threshold; deltas stopped arriving mid-call |
| **3. No screen context** | Profile + tools only — Asha asks what the user sees rather than reading it; tools still resolve real account facts | Initial snapshot never ingested; `SnapshotIngestor` failure; client never published the context channel |
| **4. Text-chat fallback** | The app's `HelpScreen` chat with the conversation context carried over — no voice, but the same brain and tools | Sustained packet loss > 15% ([docs/06 §7](06-voice-pipeline.md)); STT **or** TTS down with no provider fallback; media never establishes (no TURN, §7) |
| **5. Human handoff** | `escalate_to_human` — the terminal rung; full transcript + resolution state handed to a person | Agent cannot resolve; `SafetyLayer` trips; user asks for a human; repeated failures at any higher rung |

The ladder is **monotonic within a call** — the system does not silently climb back up mid-turn (a recovered snapshot resumes rung 1 on the *next* turn, cleanly), because flapping between "I can see your screen" and "I can't" is worse than staying honest one rung down. Rung 5 is always reachable from any rung: a human is never more than one tool call away.

### 4.1 Walking the ladder — the canonical call

Trace the canonical incident ([docs/01](01-product-and-use-case.md), canon §2) down the ladder as failures inject:

| Point in the call | Rung | What Asha does |
|---|---|---|
| Call opens, snapshot ingested | **1** | *"Hi Rajesh, I can see your ₹245 payment to Amazon Business didn't go through — your daily transaction limit was exceeded…"* — reads live screen state |
| App backgrounded mid-call; context channel silent, `seq` gap unrecovered | **2** | Stops asserting live UI: *"are you still on the payment screen?"* before acting on possibly-stale state |
| (Alt) snapshot never arrived at setup | **3** | Still greets Rajesh by name from profile, still calls `get_payment_status` / `get_wallet_balance` for real facts — but asks what he sees instead of reading it |
| 4G leg degrades past 15% loss ([docs/06 §7](06-voice-pipeline.md)) | **4** | Hands to the `HelpScreen` chat with the transcript intact — same brain and tools, no voice |
| Issue needs a person (or Rajesh asks) | **5** | `escalate_to_human` with the full transcript + the ₹245 / limit context |

At every rung Asha is still *doing her job* against real account data — she loses context fidelity and then the channel, never the ability to help or the honesty about what she can actually see.

---

## 5. Failure-mode tables

Doc-set convention: **Failure | Detection | Impact | Mitigation | Degradation** (canon §14). Pipeline-level (media/provider stream breaking mid-turn) failures live in [docs/06 §7](06-voice-pipeline.md); brain-level (LLM/tool timeout) in [docs/05 §6](05-agent-architecture.md). These are the **infrastructure and provider-outage** failures — whole subsystems degrading under load or loss.

### 5.1 Infrastructure

| Failure | Detection | Impact | Mitigation | Degradation |
|---|---|---|---|---|
| **LiveKit node loss** | Node health check fails; participants for its rooms disconnect | Every call homed on that node drops media | Multi-node cluster; clients auto-reconnect (§6, [docs/06 §6](06-voice-pipeline.md)); LiveKit re-homes/redispatches rooms; 30 s session grace holds state | Brief silence → resume on a surviving node within grace; else new session |
| **voice-worker crash mid-call** | Job/heartbeat lost; LiveKit sees the agent leave | That worker's 20–40 calls lose their agent | LiveKit **re-dispatches** to a healthy worker; the new worker rehydrates transcript window, rolling summary, and pending-confirm from the still-live `session:{id}` ([docs/06 §6.1](06-voice-pipeline.md)) | Callers hear a hiccup, then Asha bridges back; a committed mutating tool stays committed and surfaces as a digest |
| **Postgres failover** | Primary health check fails; writes error | Writes blocked during the promotion window (seconds–tens of seconds); business tool reads on the primary fail | Managed HA (replica promotion); reads continue on replicas; post-call writes retry with backoff; live-call hot path is Redis, not Postgres | Rung 2–3: a tool read that can't reach the DB → Asha says she can't fetch that *right now* rather than inventing it |
| **Redis eviction / outage** | `enforce_rate` raises on connect; session reads miss | New sessions can't be admitted; evicted `session:{id}` can't rehydrate on reconnect | **Fail closed** on session-create → `503 SESSION_CAPACITY` ([docs/04 §7.5](04-backend-architecture.md)); `noeviction` (or dedicated session DB) so live session hashes aren't evicted under memory pressure; cluster for capacity | In-flight calls **continue memory-light** on in-process short-term state — but lose cross-reconnect resume and summary persistence; new calls rejected |
| **Connection exhaustion** | Postgres `too many connections`; pgbouncer pool saturation | New DB work (tool reads, post-call writes) blocks | **pgbouncer** transaction pooling in front of primary/replicas; worker DB pools capped well below the server slot count | Tool reads queue then fail → rung 2–3; post-call writes retry after the call ends |
| **Autoscaler / dispatch lag** | Concurrent-call gauge spikes faster than workers boot | Session-creates arrive with no free worker slot | Pool headroom buffer; a warm-pool of pre-booted workers; admission 503 (§6) sheds overflow rather than over-committing | New callers get `503 SESSION_CAPACITY` + `Retry-After` for seconds until the pool catches up |
| **Thundering herd on restart** | Deploy/crash → mass worker restart, mass client reconnect, cache-warm storm, provider request spike | Reconnect + cold-cache + provider-rate spike can cascade into a second outage | Rolling/staggered restarts; **jittered reconnect backoff**; readiness gate so the LB withholds traffic until a worker is warm; LiveKit spreads re-dispatch; connection-pool caps | Slower call-setup for a few seconds post-deploy; individual calls degrade per the ladder rather than the fleet browning out together |

### 5.2 Providers

Provider fallback is layered per canon §3 (every provider is pluggable behind a `Protocol`) and the OpenRouter `models: [...]` array ([docs/04 §4](04-backend-architecture.md)).

| Failure | Detection | Impact | Mitigation | Degradation |
|---|---|---|---|---|
| **Deepgram (STT) outage** | Stream connect fails / repeated close events | No transcription | Reconnect immediately ([docs/06 §7](06-voice-pipeline.md)); config'd fallback STT via `SttProvider`; per-call watchdog caps retries | One honest re-ask, then **rung 4** (text) if no STT path resolves |
| **ElevenLabs (TTS) outage** | 5xx / stream abort on `tts.first_byte` | Asha has no voice | Retry the sentence on a fallback voice id; fallback TTS provider via `TtsProvider`; per-sentence dispatch means only the failed sentence re-synths | Brief gap → backup voice; if none, **rung 4** with context intact |
| **OpenRouter model failure** | Primary model 5xx on the streaming call | One turn's generation fails | `models: [...]` **reroutes at the provider edge inside the same HTTP call** ([docs/04 §4](04-backend-architecture.md), [docs/05 §3.4](05-agent-architecture.md)); the fallback model's slug lands on the span as `model` | Silent, sub-turn reroute; a filler phrase covers a slow retry ([docs/06 §3.2](06-voice-pipeline.md)) |
| **OpenRouter *gateway* outage** | Connect failures to the gateway host itself | The `models: [...]` array **does not help** — same gateway | Honest limitation for the demo; production adds a **secondary gateway or direct-to-provider** fallback path behind `LLMProvider` | One filler, then a spoken apology → **rung 5** (human handoff) |

The gateway-outage row is the deliberate honest gap: the fallback array covers *model and upstream-provider* failures behind OpenRouter, not a failure of OpenRouter itself. A single gateway is a single point of failure the demo accepts and production removes.

---

## 6. Backpressure & admission control

Two guards keep load from becoming cost or collapse: an **admission gate** at session creation, and **per-call watchdogs** for the duration of each call.

**Admission at session-create.** Minting a session is the expensive act — it reserves a room, prefetches context, and warms the LLM cache ([docs/04 §6](04-backend-architecture.md)). agent-api tracks in-flight concurrency against the worker pool's registered capacity. When the pool has no free slot, agent-api does **not** mint a room it can't serve — it returns **`503 SESSION_CAPACITY` with a `Retry-After` header**, and the app shows "we're busy, try again in a moment" with nothing to clean up. This is distinct from the per-user `429 RATE_LIMITED` sliding window ([docs/04 §6](04-backend-architecture.md)): the 429 stops one user abusing cost; the 503 stops the *fleet* over-committing. A short bounded queue may absorb a transient spike, but the queue has a hard depth and a deadline — a request that would wait past the caller's patience gets the 503 immediately rather than a stale room later.

```mermaid
flowchart TB
    REQ["POST /v1/sessions"] --> RL{"per-user window OK? (rate key)"}
    RL -- "no" --> R429["429 RATE_LIMITED + Retry-After"]
    RL -- "yes" --> CAP{"pool has free capacity?"}
    CAP -- "no" --> R503["503 SESSION_CAPACITY + Retry-After"]
    CAP -- "yes" --> MINT["mint room + prefetch context + warm cache"]
    MINT --> OK["200: session_id, livekit_token"]
```

The two gates run in order and for different reasons: the per-user check keys on the verified `sub` (it must run *after* auth, [docs/04 §4.1](04-backend-architecture.md)) and defends cost per caller; the capacity check defends the fleet and is the one that scales with §2. Neither gate ever leaves a half-minted room — both reject before any room, prefetch, or cache warm happens.

**Per-call watchdogs.** Every live call runs two background guards; both wind the call down *gracefully* (spoken close + post-call pipeline), never a hard kill:

| Watchdog | Limit | Action on breach |
|---|---|---|
| **Max call duration** | ~15 min hard cap (`MAX_CALL_DURATION_SECONDS`) | A support call this long should be a human's — Asha wraps and offers `escalate_to_human` (rung 5) |
| **Cost cap** | `CALL_COST_CAP_USD` = **$1.00** ([docs/04 §3](04-backend-architecture.md), [docs/05 §3.8](05-agent-architecture.md)) | Running `cost_usd` in the `session:{id}` hash crosses the cap → wind down; a runaway loop can't burn unbounded provider spend |

The cost cap is the runaway backstop the whole cost story leans on: a single misbehaving call is bounded at ~3× the ≈ $0.30 expected cost, so no bug turns 10k healthy calls' economics upside down.

---

## 7. Service-level objectives (production evolution — future)

SLOs, alerting, and error budgets are **explicitly deferred** (canon §4.5: *"Deferred: eval platform, alerting/SLOs, session replay"*). The instrumentation to measure them ships in the demo — the per-turn OTel spans and structlog events ([docs/04 §7](04-backend-architecture.md)) — but the targets, alerts, and budgets are Phase 6 work. Stated here as the intended production contract, marked **future**:

| SLO | Target | Measurement source | Status |
|---|---|---|---|
| **Answer latency (turn)** | p95 ≤ 2.0 s (p50 ≤ 1.0 s) | `turn` span `turn_ms`, Tempo TraceQL quantile ([docs/06 §3](06-voice-pipeline.md), canon §7) | Future |
| **Call-setup success rate** | ≥ 99.0% | session-create logs × client media-established event; ratio of calls that reach first greeting | Future |
| **Mid-call drop rate** | ≤ 1.0% of calls | `call_ended` reason distribution in Loki — share ending `dropped` vs graceful | Future |

Each target has a canonical reference line the demo dashboard already draws ([docs/04 §7.3](04-backend-architecture.md)); the future work is attaching alerts and an error budget to them, not building new measurement. Once live, each target becomes an **error budget** — a 99.0% call-setup SLO permits ~1% of setups to fail per window, and that budget, not intuition, gates whether to ship a risky change or freeze and stabilize. The honesty caveat from [docs/06 §3.1](06-voice-pipeline.md) carries: the demo's latency numbers were measured on a LAN with providers over the public internet, so the p95 target becomes a real SLA only once measured against production traffic on Indian mobile networks.

---

## 8. Load-testing plan (Phase 6)

The demo runs one call, so the entire capacity model above (§2) is **unvalidated by construction** — the 20–40 calls/worker estimate and the provider ceilings are numbers the load harness exists to confirm. Sketch, marked **Phase 6** ([docs/16](16-tech-stack.md), canon §13):

- **Synthetic caller harness.** Spins up N headless LiveKit participants that mint real sessions (`POST /v1/sessions`) and publish **pre-recorded PCM audio fixtures** — the canonical call turns ([docs/04 §8](04-backend-architecture.md) e2e fixtures) played on a loop with realistic inter-turn pacing — into real rooms. Reuses the same seeded scenario so assertions are deterministic.
- **Assertions per synthetic call.** Turn latency against the budget (fail if p95 > 2.0 s), call-setup success, correct resolution path (`request_limit_increase` → confirm → summary), and no dropped/interrupted turns beyond tolerance.
- **Ramp to the knee.** Increase concurrency stepwise to find (a) the **worker CPU knee** — where per-turn latency degrades as VAD/turn-detector inference saturates 4 vCPU, confirming or correcting the 20–40 estimate — and (b) the **provider ceiling** — the concurrency where Deepgram/ElevenLabs/OpenRouter start returning rate-limit errors, confirming §2.1.
- **Run target.** Against a staging stack sized like §3.1 at a fraction of 10k; extrapolate the two knees to the full target. The output feeds the autoscaler thresholds and the admission-control capacity number (§6).

| Ramp step | What it finds |
|---|---|
| 0 → worker saturation | The **worker CPU knee** — concurrency where turn p95 degrades, confirming/correcting 20–40 calls/worker (§2.2) |
| worker-knee → provider errors | The **provider ceiling** — concurrency where Deepgram/ElevenLabs/OpenRouter return rate-limit errors, confirming §2.1 |
| soak at ~80% of the knee | Slow creep — memory growth, connection-pool drift, span/log backpressure over a long run |

The harness deliberately drives **real audio through the real SFU and real providers**, not mocks — mocking the providers would hide exactly the ceiling the test exists to find.

---

## Decisions this document exports

| Fixed here | Value | Heaviest consumer |
|---|---|---|
| Scaling axis | Concurrent calls, not RPS; derived rates fall out of call duration | [docs/02](02-system-architecture.md), [docs/16](16-tech-stack.md) |
| Worker capacity estimate | ~20–40 concurrent calls / 4-vCPU worker (load-test to confirm); call→worker affinity | [docs/06](06-voice-pipeline.md), autoscaler |
| Provider ceilings as the bottleneck | ElevenLabs concurrency binds first; enterprise/committed throughput required at scale | [docs/16](16-tech-stack.md) |
| **Degradation ladder** (canonical home) | full → stale-flag → no-screen → text → human; monotonic within a call | [docs/05](05-agent-architecture.md), [docs/06](06-voice-pipeline.md), [docs/02](02-system-architecture.md) |
| Admission control | `503 SESSION_CAPACITY` + `Retry-After` when pool exhausted; distinct from `429` per-user | [docs/04](04-backend-architecture.md), [docs/13](13-api-contracts.md) |
| Per-call watchdogs | ~15 min max duration; `CALL_COST_CAP_USD` = $1.00; graceful wind-down | [docs/04](04-backend-architecture.md), [docs/05](05-agent-architecture.md) |
| SLO targets (future) | turn p95 ≤ 2.0 s; call-setup ≥ 99.0%; drop ≤ 1.0%; measured via OTel/Loki | Phase 6 |
| Load-harness contract | Synthetic PCM-fixture callers through real SFU/providers; ramp to worker + provider knees | Phase 6 |
