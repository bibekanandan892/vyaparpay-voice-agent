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
2. **No call has completed.** Check "Calls finalized (window)" on the cost
   board and "Recent turns" on the latency board. Those two panels exist
   specifically to tell *no traffic* apart from *no data*.
3. **The metrics generator is not recording.** If the raw-span tables have
   rows but the graphs are empty, the problem is
   [`infra/docker/tempo/tempo.yaml`](../infra/docker/tempo/tempo.yaml), not
   the trace pipeline. Its two traps are documented in that file and
   asserted by tests; the quickest manual check is to run
   `{span:name="turn"} | count_over_time()` in Grafana's Explore view.
4. **The time range is wider than Tempo's retention** (24 h) or than the
   metrics query limit (`query_frontend.metrics.max_duration`, also 24 h).

---

## Board 1 - Turn Latency

Panels are split into two groups on purpose. The **waterfall** group uses
TraceQL *metrics* and therefore depends on Tempo's metrics generator; the
**raw spans** group uses plain trace search and works without it. That split
is the built-in diagnostic described above.

| Panel | Query source | Reference line | Notes |
|---|---|---|---|
| Turn stage latency p95, stacked | `stt.final`, `context.build`, `llm.total`, `tts.first_byte`, `tool.exec.*` span **durations** | docs/06 §4 per-stage p95 | `llm.ttft` is excluded — it is a *child* of `llm.total`, and stacking both double-counts the same wall time |
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

### The waterfall cannot reach the ~1,900 ms p95 total

Two of docs/06 §4's seven budgeted stages emit no span at all:

- **VAD endpoint detection** (250/400 ms) — it is the `endpoint_ms`
  *attribute* on the outer `turn` span, not a span of its own.
- **Opus encode + network + jitter buffer** (75/140 ms) — `docs/06:188`
  states this is measured client-side from WebRTC `getStats` and is never a
  Tempo span.

So expect the span-derived stack to land near **575 ms p50 / 1,360 ms p95**.
That gap is expected. The full-turn-duration panel is the one to read
against the 1.0 s / 2.0 s SLO, because the outer `turn` span *does* span the
whole thing.

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
(`conversation_manager.py:413`, inside `_run_tool_loop`'s `while True:` at
`conversation_manager.py:306`), and each call re-sets the same attribute key
on the same `turn` span. OTel's `set_attribute` is last-write-wins, so a
`turn` span carries **only its final round's** tokens and cost. Any turn
that used a tool undercounts.

`call_costs` has no such problem: `CostTracker.finalize()` writes
`input_tokens=sum(t.input_tokens for t in self._turns)` and the analogous
cost sums (`cost_tracker.py:240-271`), and the `ck_call_costs_total_usd`
CHECK constraint (`app/models/orm.py:322`) forces the component columns to
agree with `total_usd`. That is the table docs/16 §5's ≈$0.30 figure is
about.

### ⚠ The cost panels cannot hit $0.30 today

`CostTracker.finalize()` hardcodes **five of the six cost components to
zero** (`cost_tracker.py:260-269`, "Judgment call #6: Phase 2 is
text-only"):

| Component | docs/16 §5 budget (cached) | Written today |
|---|---|---|
| STT — Deepgram | $0.04 | **0** |
| LLM dialogue | $0.09 | real |
| LLM utility | $0.01 | real |
| Embeddings | <$0.001 | **0** |
| TTS — ElevenLabs | $0.15 | **0** |
| Turn infra | $0.00 | not passed (column default 0) |
| **Total** | **≈$0.30** | **≈$0.10** |

So a reading of ~$0.10 is a **missing-component signal, not an
under-budget win** — and TTS, the single largest line item at ~50% of a
real call, is one of the zeros. The "Cost components per call (stacked)"
panel exists to make that visible rather than letting the total quietly
mislead.

**This blocks part of the docs/17 §2.5 exit criterion.** That criterion asks
for a dashboard "matching the ≈$0.30 (~₹25) canonical figure". The board can
*draw* the $0.30 and $0.35 lines and does; the data cannot reach them until
whichever batch owns STT/TTS/embeddings cost attribution fills those
columns in. That is application work, not dashboard work.

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
| `turn` (outer) | `turn_ms` | `app/voice/worker.py:356` |
| `turn` (inner) | `session_id` | `app/agent/conversation_manager.py:220` |
| `turn` (inner) | `turn_no` | `app/agent/conversation_manager.py:221` |
| `turn` (inner) | `turn.failed` | `app/agent/conversation_manager.py:235` |
| `turn` (inner) | `turn.affirmed` | `app/agent/conversation_manager.py:256` |
| `turn` (inner) | `turn.over_budget` | `app/agent/conversation_manager.py:283` |
| `turn` (inner) | `turn.tool_loop_bound_hit` | `app/agent/conversation_manager.py:350` |
| `turn` (inner) | `turn.output_blocked` | `app/agent/conversation_manager.py:469` |
| `turn` (inner) | `input_tokens` | `app/agent/cost_tracker.py:175` |
| `turn` (inner) | `output_tokens` | `app/agent/cost_tracker.py:176` |
| `turn` (inner) | `cost_usd` | `app/agent/cost_tracker.py:177` |
| `turn` (inner) | `model` | `app/agent/cost_tracker.py:178` |
| `turn` (inner) | `cost_estimated` | `app/agent/cost_tracker.py:181` |
| `stt.final` | `is_endpoint` | `app/voice/worker.py:391` |
| `stt.final` | `stt_ms` | `app/voice/worker.py:398` |
| `stt.final` | `text_len` | `app/voice/worker.py:399` |
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
| `llm.total` carries `llm_ms`, `input_tokens`, `output_tokens`, `cost_usd` | `llm_ms` never emitted; the token/cost trio lands on the **`turn`** span, because `conversation_manager.py:413` passes the turn span into `record_turn()` |
| `tool.exec.<name>` carries `tier` | not emitted there; only `llm.ttft`/`llm.total` carry `tier` |

This document does not fix docs/04 — that is a doc change outside this
batch's scope — but the boards are built against the code, not against that
table.

---

## The guard against silent drift

[`tests/obs/test_dashboards.py`](tests/obs/test_dashboards.py) — 17 tests,
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
  relationship are asserted.

Each test was proven non-vacuous by breaking the thing it covers and
confirming exactly that test failed: renaming `SPAN_TURN`, renaming the
`tool.exec.` prefix, deleting the `cost_usd` emission site, flipping
`filter_server_spans` back to `true`, changing the compose mount target,
renaming a SQL column, and pointing a panel at an unprovisioned datasource.

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
4. **The unit of `quantile_over_time(span:duration, …)`.** Panels assume
   **seconds** (`"unit": "s"`, thresholds written as `0.45`/`0.9`/`1`/`2`).
   If Tempo returns nanoseconds, every threshold line on the latency board
   is off by 10⁹ — the graphs will still draw, just uselessly. **Check this
   first.**
5. **That `filter_server_spans: false` actually admits INTERNAL child
   spans.** This was derived from reading Tempo's `filterBatches()` source,
   not from a doc that states the INTERNAL case.
6. **The Postgres datasource connecting**, and `$__timeFilter` behaving on
   `created_at`.
7. **Whether the metrics generator starts cleanly** with
   `storage.remote_write` absent.
8. **`docker compose config -q`.** CI's `docker-gated` job runs it on every
   PR, so this is covered *there*, just never locally. The
   `depends_on`-shape change on `grafana` (short list → map form, needed to
   add a `service_healthy` condition) is the part worth watching.

Items 1–7 belong on the project's needs-human ledger.
