# Observability dashboards

Phase-5 Batch 5. Two provisioned Grafana boards over the traces the backend
already emits, plus the per-call cost ledger in Postgres:

| Board | UID | Source | Answers |
|---|---|---|---|
| **VyaparPay - Turn Latency** | `vyaparpay-turn-latency` | Tempo | Where did this turn's ~1 s go? |
| **VyaparPay - Cost & Token Budgets** | `vyaparpay-cost-tokens` | Postgres + Tempo | What did this call cost, and did the prompt fit its budget? |

Everything is provisioning-as-code under
[`infra/docker/grafana/`](../infra/docker/grafana/). Nothing is hand-clicked,
because the Grafana container runs with **no persistent volume** — anything
built in the UI dies with `docker compose down`. The boards are provisioned
`allowUiUpdates: false` for the same reason: editing one in the browser
produces a change that silently reverts on the next container start.

> **Read [What is NOT verified](#what-is-not-verified) before trusting a
> panel.** These boards were authored on a machine with no Docker daemon
> and no Docker CLI. Every query was cross-checked against the emitting
> code and is guarded by tests, but **no panel has ever been rendered.**

---

## Bring it up

```bash
# 1. secrets, once (see infra/README.md for the full walkthrough)
cp backend/.env.example backend/.env

# 2. point the backend's OTLP exporter at Tempo. It ships EMPTY, and empty
#    disables export entirely - this is the single most common reason the
#    latency board is blank.
#    In backend/.env:
#      OTEL_EXPORTER_OTLP_ENDPOINT=http://tempo:4317

# 3. the obs profile brings up tempo + grafana on top of the base stack
docker compose --profile obs up -d

# 4. Grafana, anonymous admin, no login form
open http://127.0.0.1:3000
```

Both boards appear in the **VyaparPay** folder. Postgres is a `depends_on`
of Grafana now, because the cost panels query it directly.

### If a panel is empty

Work down this list — it is ordered by how often each one is the cause.

1. **`OTEL_EXPORTER_OTLP_ENDPOINT` is still empty in `backend/.env`.** No
   spans are being exported at all; Tempo is idle, not broken. Restart
   `agent-api` and `voice-worker` after setting it.
2. **The call was a voice call, and nothing finalized it.** The cost
   board reads `call_costs`, and that table is written only by
   `CostTracker.finalize()`. Grep for callers: `scripts/demo_cli.py` is
   the only one. `DELETE /v1/sessions/{id}` publishes `"end"` on
   `session_control:{id}` for the worker to run finalize-then-end
   (`app/api/routes/sessions.py` judgment call 1), but no subscriber to
   that channel exists in `app/voice/`, and `CallSession.close()` does
   not finalize either. So a voice call currently produces spans and
   **nothing else durable**: no cost row, and no `session:{id}:turns`
   records either — `RedisClient.append_turn` has exactly one caller,
   inside `finalize()` itself, so the per-turn ledger dies with the
   process too. (`ConversationManager` writes the 8-turn `transcript`
   field on the `session:{id}` hash; that is a different key and a
   rolling window, not the ledger.) Wiring the worker's post-call
   pipeline is a separate task from pricing the components.
3. **No call has completed.** Check "Calls finalized (window)" on the cost
   board and "Recent turns" on the latency board. Those two panels exist
   specifically to tell *no traffic* apart from *no data*.
4. **The metrics generator is not recording.** If the raw-span tables have
   rows but the graphs are empty, the problem is
   [`infra/docker/tempo/tempo.yaml`](../infra/docker/tempo/tempo.yaml), not
   the trace pipeline. Its two traps are documented in that file and
   asserted by tests; the quickest manual check is to run
   `{span:name="turn"} | count_over_time()` in Grafana's Explore view.
5. **The time range is wider than Tempo's retention** (24 h) or than the
   metrics query limit (`query_frontend.metrics.max_duration`, also 24 h).

---

## Board 1 - Turn Latency

Panels are split into two groups on purpose. The **waterfall** group uses
TraceQL *metrics* and therefore depends on Tempo's metrics generator; the
**raw spans** group uses plain trace search and works without it. That split
is the built-in diagnostic described above.

| Panel | Query source | Reference line | Notes |
|---|---|---|---|
| Turn stage latency p95, stacked | `stt.final`, `context.build`, `llm.ttft`, `tts.first_byte`, `tool.exec.*` span **durations** | docs/06 §4 per-stage p95 | Stacks exactly the four spans §4.1 gives a budget row to. `llm.total` is excluded — [see below](#why-llmtotal-is-not-in-the-stack) |
| Full turn duration p50/p95 | outer `turn` span duration | p50 ≤ 1.0 s, p95 ≤ 2.0 s (docs/15 §7) | Selected by `span.turn_ms != nil` — see [the two `turn` spans](#the-two-turn-spans) |
| LLM time-to-first-token | `llm.ttft` span duration, grouped by `span.tier` | 450 ms p50 / 900 ms p95 (docs/06 §4) | The p50 budget assumes a **warm** cached prefix; docs/06 says a cold prefix roughly doubles it |
| Tool execution p95 by tool | `tool.exec.*` span durations | 2,000 ms hard ceiling (docs/10) | Seeded tools really run 5–15 ms (reads) / ~40 ms (writes), so anything near the line is an outlier |
| Recent turns | raw spans | — | First panel to check when the graphs are blank |
| Recent tool executions | raw spans | — | Columns are exactly what `ToolExecutor` emits |
| Turns that hit a guard rail | raw spans, the five `turn.*` booleans | — | Empty is healthy; cross-check "Recent turns" to distinguish from no traffic |
| Recent STT / TTS | raw spans | STT 150 ms p95, TTS TTFB 250 ms p95 | A `tts.first_byte` span with no `tts_ttfb_ms` is a documented alignment-only stream, not an error |

### Stages time by span *duration*, not by `*_ms` attributes

Deliberate. docs/04 §7.2's span table lists `ctx_ms`, `ttft_ms` and
`llm_ms`, but **no code emits any of them** (see
[Doc drift found](#doc-drift-found-docs04-72)). Span duration is always
recorded by the SDK, so it is the only reliable timing source for those
stages.

### Why `llm.total` is not in the stack

It is tempting — it is the span that wraps the LLM call — but it is not a
stage of the budget the panel is read against, and stacking it is wrong
twice over:

- docs/06 §4 defines the turn as **"user stops speaking → agent audio
  starts."** `llm_router.py:395` holds `total_span` open across the entire
  generation (it wraps `_forward_events`), which by design keeps running
  *after* audio starts — that is the whole point of sentence-level dispatch
  (docs/06 §4.2, and §4.1's TTS row: "later sentences overlap generation").
  So it measures a longer window than the thing it would be compared to.
- Because of that same overlap it double-counts wall time already
  attributed to `tts.first_byte`.
- docs/06 §4.1 maps the 450/900 row to the **`llm.ttft` span**, explicitly.
  There is no docs/06 budget for total generation time.

Left in, the stack would overshoot on a perfectly healthy turn while the
full-turn panel sat comfortably inside 2.0 s — two panels disagreeing, with
the header telling the operator to trust the stack.
`test_waterfall_stacks_exactly_the_stages_that_emit_a_span` now enforces
1:1 correspondence with §4.1's span-mapped rows.

### The waterfall cannot reach the ~1,900 ms p95 total

**Three** of docs/06 §4's seven budgeted stages emit no span:

- **VAD endpoint detection** (250/400 ms) — it is the `endpoint_ms`
  *attribute* on the outer `turn` span, not a span of its own.
- **First sentence chunked → TTS dispatch** (10/20 ms) — docs/06 §4.1 marks
  it "span-adjacent; measured as the gap between first token and first TTS
  request".
- **Opus encode + network + jitter buffer** (75/140 ms) — `docs/06:188`
  states this is measured client-side from WebRTC `getStats` and is never a
  Tempo span.

So the stack is exactly the four remaining stages, and should land near
**665 ms p50 / 1,340 ms p95** — that is 80+15+450+120 and 150+40+900+250,
which together with the three above reconcile to §4's ~1,000 / ~1,900
totals. That gap is expected. The full-turn-duration panel is the one to
read against the 1.0 s / 2.0 s SLO, because the outer `turn` span *does*
span the whole thing.

These figures are no longer prose-only:
`test_documented_expected_stack_equals_the_budget_sum` recomputes them from
a transcription of docs/06 §4 and fails if this document or the on-board
header drifts from the arithmetic.

### A tool turn runs some of these stages more than once

The same multi-round fact that pushed cost to Postgres applies to latency,
and is easier to miss. `_run_tool_loop`'s `while True:`
(`conversation_manager.py:322`) opens a fresh LLM round per tool round, so
**`llm.ttft` and `tool.exec.*` occur several times within a single turn** —
docs/06 §4.3 walks through exactly this for the canonical Rajesh Turn 3
(two parallel read tools, then "a short LLM continuation to reason over the
results").

A per-span p95 renders one bar each regardless of how many times the stage
ran. So on tool turns the stack **understates** real serial LLM time by
roughly a full round, and an operator reading it sees headroom that is not
there. When the question is "did this turn fit the SLO", read the
full-turn-duration panel, which measures the outer `turn` span and therefore
includes every round.

### The two `turn` spans

On the voice path there are **two** spans named `turn` per turn, nested:

- the **outer** one, opened by `VoiceAgentWorker._run_turn` at the endpoint
  decision (`app/voice/worker.py:339`);
- the **inner** one, opened by `ConversationManager.on_stt_final`
  (`app/agent/conversation_manager.py:219`). The worker's own comment at
  `worker.py:337` says this nesting is intended.

A bare `{span:name="turn"}` matches both and mixes full-turn duration with
brain-only duration. Every panel disambiguates by attribute presence:

- outer only: `turn_ms`, `endpoint_ms`, `interrupted`
- inner only: `input_tokens`, `output_tokens`, `cost_usd`, `model`

---

## Board 2 - Cost & Token Budgets

### Why cost comes from Postgres, not from the spans

The `turn` span *does* carry `cost_usd`. Summing it would still be wrong.

`CostTracker.record_turn()` is called **once per LLM round**
(`conversation_manager.py:429`, inside `_run_tool_loop`'s `while True:` at
`conversation_manager.py:322`), and each call re-sets the same attribute key
on the same `turn` span. OTel's `set_attribute` is last-write-wins, so a
`turn` span carries **only its final round's** tokens and cost. Any turn
that used a tool undercounts.

`call_costs` has no such problem: `CostTracker.finalize()` writes
`input_tokens=sum(t.input_tokens for t in self._turns)` and the analogous
cost sums (`cost_tracker.py:440-468`), and the `ck_call_costs_total_usd`
CHECK constraint (`app/models/orm.py:323-326`) forces the component columns to
agree with `total_usd`. That is the table docs/16 §5's ≈$0.30 figure is
about.

### How to read a component that shows $0

Five of the six columns are priced from config unit prices
(`app/config.py`: Deepgram per audio-minute, ElevenLabs per character,
embeddings and LLM per million tokens) against usage the producing stage
reports; `turn_infra_usd` is written as a literal $0, because coturn is
self-hosted and docs/16 §5 budgets its marginal per-call cost at zero.

A priced stage that never reports contributes $0 — which is **correct for
a call that never ran that stage** and a **defect for one that did**, and
the row alone cannot tell those apart. `finalize()`'s
`cost_tracker.finalized` log line carries `recorded_stages` /
`unrecorded_stages` for exactly that question; check it before concluding
a $0 column is broken.

Which stages report today:

| Component | docs/16 §5 budget (cached) | Reported by |
|---|---|---|
| STT — Deepgram | $0.04 | `SttSupervisor` → worker → brain (voice calls only) |
| LLM dialogue | $0.09 | `CostTracker.record_turn` |
| LLM utility | $0.01 | `CostTracker.record_turn` |
| Embeddings | <$0.001 | nothing yet — see below |
| TTS — ElevenLabs | $0.15 | `SpeechDispatcher` → worker → brain (voice calls only) |
| Turn infra | $0.00 | passed as $0 (self-hosted coturn, docs/16 §5) |

Three things an operator should know before reading these panels:

1. **Embeddings will read $0 on every call.** `OpenAIEmbeddings` is not
   constructed in any composition root, so the stage does not run at all;
   it appears in `unrecorded_stages` on every finalize. docs/16 §5 budgets
   it at <$0.001, so this moves the total by less than a rounding step.
2. **A text-only call legitimately has no STT or TTS.** `scripts/demo_cli.py`
   is text-only, so its rows show $0 for both and its total is LLM-only.
   Voice calls are the ones the ≈$0.30 line is drawn for.
3. **And no voice call writes a row yet at all** — nothing in the worker
   calls `finalize()`, per ["If a panel is empty"](#if-a-panel-is-empty)
   item 2. So the STT and TTS rows in the table above describe metering
   that runs and feeds `call_total()`'s live budget guard, but that has no
   row to land in until the worker's post-call pipeline is wired. Until
   then every row on this board comes from `scripts/demo_cli.py`, whose
   total is LLM-only by construction.

Neither the ≈$0.30 nor the ≈$0.35 reference line has been checked against
a rendered panel fed by a real call — see [What is NOT verified](#what-is-not-verified).
What *is* checked is the arithmetic behind it:
`tests/agent/test_cost_tracker.py` replays docs/16 §5's canonical
5-minute call through `CostTracker` and asserts every component, the
total, and that TTS is the largest line item.

**Expect the canonical call to price at $0.2809, not $0.30.** That test
records the exact figure. Two reasons, both in the test's own docstrings:
docs/16 §5's per-component column is rounded to cents and sums to $0.29
before it is quoted as "≈$0.30"; and `record_turn` prices cached input at
a single rate, while docs/16 §5's assumptions price cache *writes* at
1.25×, a $0.0048 premium on the canonical call's one prefix write.
Charging that premium needs `cache_creation_input_tokens` plumbed as its
own usage field. So a real 5-minute call landing near $0.28 is on budget;
one landing near $0.10 is a missing media component.

### Panels

| Panel | Source | Reference line |
|---|---|---|
| Cost per call vs the canon line | `call_costs.total_usd` | $0.30 cached / $0.35 uncached (docs/16 §5) |
| Cost components per call (stacked) | the six component columns | TTS should be ~50% of a real call |
| Mean cost per call (USD) | `avg(total_usd)` | same |
| Mean cost per call (INR) | `avg(total_usd) * 83.3333` | ₹25 |
| Calls finalized (window) | `count(*)` | zero = no traffic, check this first |
| Prompt-cache hit share | `sum(cached_input_tokens)/sum(input_tokens)` | ~64% implied by docs/16 §5; caching is worth ~$0.06/call |
| Input tokens per turn vs budget | `turn` span `input_tokens` | 2,450 slot sum / 2,500 target / 3,000 hard cap (docs/11 §1) |
| Output tokens per turn vs budget | `turn` span `output_tokens` | 150 (docs/11 §1) |
| Tokens per CALL | `call_costs` token columns | call-scale (~44k in / ~1.6k out), **not** the per-turn lines |
| Recent turns - tokens, model, cost | raw spans | works without the metrics generator |

**On the INR rate.** No doc in this repo states a USD→INR rate. The
`83.3333` in that panel is *derived* from docs/16 §5's own paired figures
($0.30 ↔ ₹25) so the panel agrees with canon by construction. It is not a
live FX rate. If canon's pairing changes, change it in the panel and here
together.

**On the per-turn token panels.** They read the `turn` span's
`input_tokens`, which is the turn's **last** LLM round (see above). For a
prompt-budget question that is the right number — the last round carries the
most context, since tool results have been appended, so it is the turn's
*largest* prompt. For token *totals* it is the wrong number; use the
per-call panel.

---

## Where every number comes from

Every span name and attribute the boards query was grepped against its
emitting call site. The full inventory is 25 distinct attribute keys; these
are the ones the dashboards actually use:

| Span | Attribute | Emitted at |
|---|---|---|
| `turn` (outer) | `session_id` | `app/voice/worker.py:340` |
| `turn` (outer) | `turn_no` | `app/voice/worker.py:341` |
| `turn` (outer) | `endpoint_ms` | `app/voice/worker.py:342` |
| `turn` (outer) | `interrupted` | `app/voice/worker.py:355` |
| `turn` (outer) | `turn_ms` | `app/voice/worker.py:387` |
| `turn` (inner) | `session_id` | `app/agent/conversation_manager.py:236` |
| `turn` (inner) | `turn_no` | `app/agent/conversation_manager.py:237` |
| `turn` (inner) | `turn.failed` | `app/agent/conversation_manager.py:251` |
| `turn` (inner) | `turn.affirmed` | `app/agent/conversation_manager.py:272` |
| `turn` (inner) | `turn.over_budget` | `app/agent/conversation_manager.py:299` |
| `turn` (inner) | `turn.tool_loop_bound_hit` | `app/agent/conversation_manager.py:366` |
| `turn` (inner) | `turn.output_blocked` | `app/agent/conversation_manager.py:493` |
| `turn` (inner) | `input_tokens` | `app/agent/cost_tracker.py:249` |
| `turn` (inner) | `output_tokens` | `app/agent/cost_tracker.py:250` |
| `turn` (inner) | `cost_usd` | `app/agent/cost_tracker.py:251` |
| `turn` (inner) | `model` | `app/agent/cost_tracker.py:252` |
| `turn` (inner) | `cost_estimated` | `app/agent/cost_tracker.py:255` |
| `stt.final` | `is_endpoint` | `app/voice/worker.py:422` |
| `stt.final` | `stt_ms` | `app/voice/worker.py:429` |
| `stt.final` | `text_len` | `app/voice/worker.py:430` |
| `llm.ttft` | `tier` | `app/agent/llm_router.py:454` |
| `tool.exec.<name>` | `tool` | `app/agent/tool_executor.py:384` |
| `tool.exec.<name>` | `turn_no` | `app/agent/tool_executor.py:385` |
| `tool.exec.<name>` | `status` | `app/agent/tool_executor.py:386` |
| `tool.exec.<name>` | `latency_ms` | `app/agent/tool_executor.py:387` |
| `tool.exec.<name>` | `idempotency_key` | `app/agent/tool_executor.py:389` |
| `tts.first_byte` | `sentence_no` | `app/providers/elevenlabs.py:160` |
| `tts.first_byte` | `tts_ttfb_ms` | `app/providers/elevenlabs.py:171` |

Span names come from the constants at `app/obs/tracing.py:60-68`; the
`tool.exec.<name>` prefix is built at `app/obs/tracing.py:190`.

### Doc drift found (docs/04 §7.2)

docs/04 §7.2 calls its span table "frozen by canon". Six attributes in it
are **never emitted by any code path**, and one span's attributes are
attributed to the wrong span. The dashboards query none of the phantoms.

| docs/04 §7.2 claims | Reality |
|---|---|
| `context.build` carries `ctx_ms`, `slots_filled`, `degraded` | none emitted; the span carries only `session_id` (`context_builder.py:203`) |
| `llm.ttft` carries `ttft_ms`, `cache_hit` | neither emitted. `cache_hit` is on the allowlist (`tracing.py:83`) but `context_builder.py:185` explains it is deliberately never set |
| `llm.total` carries `llm_ms`, `input_tokens`, `output_tokens`, `cost_usd` | `llm_ms` never emitted; the token/cost trio lands on the **`turn`** span, because `conversation_manager.py:429` passes the turn span into `record_turn()` |
| `tool.exec.<name>` carries `tier` | not emitted there; only `llm.ttft`/`llm.total` carry `tier` |

**The same drift appears a second time**, at `docs/06-voice-pipeline.md:183`
— §4.1's LLM-TTFT row names "`llm.ttft` span; `cache_hit` attribute" as its
instrument, and `cache_hit` is the never-emitted key above. Anyone
correcting docs/04 §7.2 should fix this line in the same pass, or the claim
survives in the doc that latency work is most likely to be read from.

This document does not fix either — that is a doc change outside this
batch's scope — but the boards are built against the code, not against those
tables.

---

## The guard against silent drift

[`tests/obs/test_dashboards.py`](tests/obs/test_dashboards.py) — 21 tests,
in the normal `pytest tests` gate.

A panel querying a span or attribute nothing emits does **not** fail. It
renders an empty graph, indistinguishable from a healthy idle system. These
tests turn that into a loud CI failure:

- span names are checked against the `SPAN_*` constants, and the
  `tool.exec.` prefix is obtained by **actually opening a span** through
  `tool_span()` and reading the name back off an in-memory exporter — so
  changing the f-string breaks the test, not just changing a constant;
- attribute keys are checked against a static scan of every real
  `safe_set_attribute(...)` call site in `app/`. This is stricter than the
  `_SAFE_SPAN_ATTRIBUTE_KEYS` allowlist on purpose: `cache_hit` is
  allowlisted but never set, and testing against the allowlist alone would
  wave a permanently-empty panel through;
- SQL columns are checked against the `CallCost` ORM model, and every SQL
  target is asserted read-only;
- datasource UIDs are checked against the provisioning YAML, and the
  dashboard provider's path against the compose mount;
- `tempo.yaml`'s two traps (`filter_server_spans`, the
  `local_blocks`/`local-blocks` spelling split) and its retention-vs-window
  relationship are asserted;
- **the waterfall's relationship to the docs/06 §4 budget** is asserted
  rather than left to prose: docs/06 §4's table is transcribed into the
  test (and the transcription is itself reconciled against the table's own
  ~1,000 / ~1,900 stated totals, so a typo cannot pass), then the stacked
  span set must equal exactly the stages §4.1 maps to a span, and the
  expected stack height quoted here and on the board must equal those
  stages' budget sums. Prose-only versions of these three facts were wrong
  in the first version of this document.

Each test was proven non-vacuous by breaking the thing it covers and
confirming exactly that test failed: renaming `SPAN_TURN`, renaming the
`tool.exec.` prefix, deleting the `cost_usd` emission site, flipping
`filter_server_spans` back to `true`, changing the compose mount target,
renaming a SQL column, pointing a panel at an unprovisioned datasource,
adding `llm.total` back into the stack, restoring the wrong 575 ms figure,
restoring the wrong "two of seven" stage count, and mistyping a budget
number in the docs/06 transcription.

---

## What is NOT verified

**No panel on either board has ever been rendered.** The machine these were
authored on has no Docker daemon *and no Docker CLI at all* — so even
`docker compose config`, which CI runs on every PR, could not be run
locally.

Verified here:

- both dashboard JSON files parse, carry the required top-level keys, have
  unique UIDs, and every query panel has a description;
- every span name, span attribute, and SQL column referenced by a query
  exists in the emitting code (see the table above);
- datasource UIDs resolve to provisioned datasources of the declared type;
- the provider path matches the compose mount, on both the host and
  container side;
- `tempo.yaml` parses and carries the settings the metrics panels need;
- `pytest`, `ruff`, `mypy` all pass.

**Needs a human with Docker** — each of these is a plausible failure that no
test here can catch:

1. **That any panel renders at all.** Grafana's provisioning could reject a
   board for a reason JSON validity cannot reveal.
2. **`schemaVersion: 39` migrating cleanly on Grafana 13.1.** Grafana
   migrates older schema versions forward and no longer rejects old ones,
   but the migrated result was never seen.
3. **Every TraceQL metrics query.** Specifically `by (span:name)` —
   grouping by an *intrinsic* rather than an attribute is the least
   certain piece of syntax on the boards. If it is rejected, the stacked
   waterfall and the tool-latency panel are the ones that break.
4. **`quantile_over_time(span.input_tokens, .95)` and
   `max_over_time(span.output_tokens)`** — aggregating over a **numeric
   span attribute** is a different signature from the `span:duration`
   intrinsic every latency panel uses, and it is the entire basis of both
   token-budget panels. If the attribute form is unsupported or coerces
   differently, those two panels are empty or wrong while every latency
   panel is fine, so it will not be caught by checking the other board.
   The raw-span "Recent turns - tokens, model and cost" table is the
   fallback and needs no metrics generator.
5. **The unit of `quantile_over_time(span:duration, …)`.** Panels assume
   **seconds** (`"unit": "s"`, thresholds written as `0.45`/`0.9`/`1`/`2`).
   If Tempo returns nanoseconds, every threshold line on the latency board
   is off by 10⁹ — the graphs will still draw, just uselessly. **Check this
   first.**
6. **That `filter_server_spans: false` actually admits INTERNAL child
   spans.** This was derived from reading Tempo's `filterBatches()` source,
   not from a doc that states the INTERNAL case.
7. **The Postgres datasource connecting**, and `$__timeFilter` behaving on
   `created_at`.
8. **Whether the metrics generator starts cleanly** with
   `storage.remote_write` absent.
9. **`docker compose config -q`.** CI's `docker-gated` job runs it on every
   PR, so this is covered *there*, just never locally. The
   `depends_on`-shape change on `grafana` (short list → map form, needed to
   add a `service_healthy` condition) is the part worth watching.

Items 1–8 belong on the project's needs-human ledger.

Note what is **not** on this list: the expected stack height, the stage
count, and the set of spans the waterfall stacks. Those were prose-only
assertions in the first version of this document and two of them were
wrong; they are now recomputed from a transcription of docs/06 §4 by
`tests/obs/test_dashboards.py` and fail the gate on drift.
