# Architecture Documentation

This is the design record for **VyaparPay's screen-aware AI voice support agent** — an agent embedded in an Android merchant-payments app that sees a semantic summary of the live screen, recent actions, and recent API errors *before the user speaks*, so it opens a support call already knowing what went wrong. Start at the repository [root README](../README.md) for the elevator pitch and the demo video; this folder is the seventeen-document deep dive behind it.

**Read this with:** [docs/01](01-product-and-use-case.md) for the product and the canonical incident, [docs/16](16-tech-stack.md) for the locked stack and ADRs, and [docs/17](17-roadmap.md) for what is built versus planned.

---

## Document map

Every document is complete for **Phase 1 — Architecture** ([docs/17](17-roadmap.md) §2.1): the design a reviewer can audit before application code exists. "Complete" means the doc is written and canon-checked, not that the code it describes is built.

| # | Document | What it covers | Status |
|---|---|---|---|
| 01 | [Product & Use-Case Selection](01-product-and-use-case.md) | Weighted domain choice, VyaparPay product surface, Rajesh's canonical incident, the annotated 9-turn transcript | Complete |
| 02 | [System Architecture](02-system-architecture.md) | The two-service split (`agent-api`, `voice-worker`), component map, room and request topology | Complete |
| 03 | [Android Architecture](03-android-architecture.md) | Gradle module graph, the call UI (`SupportButton`, `ConversationOverlay`, `CallStateMachine`), foreground call service | Complete |
| 04 | [Backend Architecture](04-backend-architecture.md) | FastAPI layout, async discipline, the `app/` package split, Postgres/Redis wiring | Complete |
| 05 | [AI Agent Architecture](05-agent-architecture.md) | The intelligence loop: `SessionManager`, `ContextBuilder`, `PromptBuilder`, `ToolExecutor`, `LLMRouter`, `SafetyLayer` | Complete |
| 06 | [WebRTC Voice Pipeline](06-voice-pipeline.md) | LiveKit transport, streamed STT→LLM→TTS, barge-in, **the latency budget** (owns all latency numbers) | Complete |
| 07 | [UI Semantic Context](07-ui-semantic-context.md) | Compose semantics tree → ScreenContext IR, the ~4,000→≤300 token transform (**owns the `screen_context/v1` schema**) | Complete |
| 08 | [Context & Event Pipeline](08-context-and-events.md) | Snapshot/delta/event ingestion, the event ring buffer, the data-channel envelope | Complete |
| 09 | [Memory Architecture](09-memory-architecture.md) | Short-term / session / profile / semantic tiers, the rolling summary, pgvector retrieval | Complete |
| 10 | [Tool-Calling Architecture](10-tool-calling.md) | The 16-tool catalog, typed Pydantic contracts, confirm-required mutations, idempotency | Complete |
| 11 | [Prompt Engineering](11-prompt-engineering.md) | The slot budget, prefix caching, persona/voice rules (**owns all token numbers**) | Complete |
| 12 | [Data Models](12-data-models.md) | Pydantic + SQLAlchemy schemas, seeded fixtures, the Redis keyspace | Complete |
| 13 | [API Contracts](13-api-contracts.md) | REST endpoints, `POST /v1/sessions`, the data-channel wire formats (**co-owns the schemas**) | Complete |
| 14 | [Security](14-security.md) | Prompt-injection fencing, PII redaction, token TTL, per-session tool authorization | Complete |
| 15 | [Scalability & Reliability](15-scalability-and-reliability.md) | Failure-mode tables, the degradation ladder, single-machine → multi-node evolution | Complete |
| 16 | [Technology Stack & Decision Records](16-tech-stack.md) | Five ADRs with flip conditions, the model/pricing table, **cost per call** (owns cost numbers) | Complete |
| 17 | [Roadmap & Future Enhancements](17-roadmap.md) | Six phases sized in evenings/weekends, the MVP line, the post-Phase-6 catalog | Complete |

---

## Reading paths

Three routes, depending on why you are here.

### 5-minute recruiter path

The signature capability, then the two hard problems behind it, then the receipts.

**[01](01-product-and-use-case.md) → [07](07-ui-semantic-context.md) → [06](06-voice-pipeline.md) → [16](16-tech-stack.md)** — what the agent does and why the domain makes it load-bearing (01), the screen-to-context transform that is the headline (07), the sub-second voice pipeline that delivers it (06), and the stack, ADRs, and ≈$0.30/call cost that prove it is engineered, not hand-waved (16).

### Full engineering path

Read in dependency order — each doc leans only on ones before it, so nothing forward-references a contract you have not met yet:

**01 → 02 → 16 → 07 → 08 → 09 → 12 → 10 → 13 → 05 → 11 → 06 → 04 → 03 → 14 → 15 → 17**

Product frames the system (01→02); the stack locks the tools and models everything else assumes (16); the ScreenContext IR, event pipeline, memory tiers, and data models are the raw materials (07→08→09→12); tools and API contracts consume those materials (10→13); the agent loop and prompt assembly wire them into intelligence (05→11); the voice pipeline wraps the working brain (06); the clients host it (04→03); and the cross-cutting security, reliability, and roadmap concerns close the set (14→15→17).

### "I want to build it" path

Ordered like an implementation plan, not a narrative:

**[17](17-roadmap.md) → [13](13-api-contracts.md) → [12](12-data-models.md) → [04](04-backend-architecture.md) / [03](03-android-architecture.md)** — the phase plan and MVP line first (17), then the wire contracts and data schemas you code against (13, 12), then the backend and Android implementations that satisfy them (04, 03). Phase 2 in [docs/17](17-roadmap.md) is the concrete first-commit target.

---

## Conventions

Rules every doc in this set obeys, so cross-reading is frictionless:

- **Diagrams are inline mermaid** (fenced `mermaid` blocks) — GitHub-renderable, no external images. Node labels with spaces or `₹` are quoted.
- **Demo vs Production columns.** This is a portfolio project: the *agent stack* is real engineering, the *business it serves* is seeded fixtures. Wherever the two diverge, a "Demo" / "Production evolution" table marks the split honestly rather than pretending the backend is a bank ([docs/01](01-product-and-use-case.md) §5 is the honesty contract every doc inherits).
- **Failure-mode tables** use fixed columns: Failure | Detection | Impact | Mitigation | Degradation.
- **One owning doc per number** — the canon principle. Any latency, token, or cost figure is *defined* in exactly one place and *referenced* (never re-derived) everywhere else:

| Number | Owning doc |
|---|---|
| Latency budget (p50 ≤ 1.0 s, p95 ≤ 2.0 s; barge-in ≤ 250 ms) | [docs/06](06-voice-pipeline.md) |
| Token budget (≤ 2,500 in / ≤ 150 out per turn) | [docs/11](11-prompt-engineering.md) |
| Cost per call (≈ $0.30 / ~₹25) | [docs/16](16-tech-stack.md) |
| Wire schemas (`screen_context/v1`, `app_event/v1`, REST) | [docs/07](07-ui-semantic-context.md) + [docs/13](13-api-contracts.md) |

If a figure here disagrees with its owning doc, the owning doc wins. See the source under [protocol/](../protocol/) and [backend/app/](../backend/app/) once code lands.

---

## Glossary

Terms used across the set without re-explanation. The owning doc carries the full treatment.

| Term | Meaning |
|---|---|
| **ScreenContext** | The compact semantic snapshot of the live app screen (`screen_context/v1`) the agent reads before the user speaks — the project's signature artifact ([docs/07](07-ui-semantic-context.md)). |
| **Semantic IR** | The intermediate representation `SemanticSnapshotBuilder` produces: the raw ~4,000-token Compose tree compressed to a ≤300-token role-based summary. |
| **Barge-in** | The user interrupting the agent mid-sentence; TTS must stop within ≤ 250 ms ([docs/06](06-voice-pipeline.md)). |
| **Turn** | One exchange, measured from *user stops speaking* to *agent audio starts* — the unit the latency budget is defined over. |
| **Endpointing** | Deciding the user has finished speaking (Silero VAD + LiveKit turn detector), the event that starts the turn clock. |
| **Data channel** | LiveKit's reliable, ordered side channel (topic `ctx`) carrying in-call ScreenContext deltas and events with client-monotonic `seq` (ADR-004). |
| **Confirm-required tool** | A mutating tool (e.g. `request_limit_increase`) the agent must voice-confirm and receive an explicit "yes" for before executing ([docs/10](10-tool-calling.md)). |
| **Degradation ladder** | The ordered fallbacks when a component fails — e.g. a missing snapshot degrades the cold-open to a generic greeting rather than dropping the call ([docs/15](15-scalability-and-reliability.md)). |
| **Rolling summary** | A Haiku-generated running summary that replaces older verbatim transcript in the prompt every 6 turns to hold the token budget ([docs/09](09-memory-architecture.md)). |
| **Prefix caching** | Ordering stable prompt slots (system, persona, business rules) first so their tokens are cached across turns, cutting LLM cost and TTFT ([docs/11](11-prompt-engineering.md)). |
| **SFU** | Selective Forwarding Unit — the LiveKit media server that routes Opus audio between the app and `voice-worker` (ADR-001). |
| **VAD** | Voice Activity Detection (Silero) — detects speech boundaries for endpointing and barge-in. |
| **TTFT** | Time-to-first-token: how long until the LLM emits its first token, the pipeline's largest single latency stage ([docs/06](06-voice-pipeline.md)). |
| **Seeded data** | The staged business fixtures (Rajesh, the ₹18,450 wallet, the declined ₹245 payment) served by `agent-api` in place of a real bank — reset-scriptable, marked demo-only everywhere. |
| **voice-worker** | The LiveKit Agents (Python) process that joins the room and runs the voice pipeline; sibling to `agent-api`. "The framework moves audio; we own the intelligence." |
