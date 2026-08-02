# Demo — the canonical Rajesh incident, resolved end to end

This walks through `scripts/demo_cli.py`, the Phase-2 portfolio milestone
([docs/17-roadmap.md](../docs/17-roadmap.md) §2.2): a screen recording of
a text chat resolving Rajesh's ₹245 declined-payment incident with real
tool calls, proving the agent brain works before any audio pipeline
exists. The CLI is a stand-in for the not-yet-built voice transport
(Phase 3) — it drives the exact same `ConversationManager` a real call
will, over stdin/stdout instead of an audio pipeline.

## Prerequisites

- Docker (for Postgres + Redis)
- A real `OPENROUTER_API_KEY` ([openrouter.ai](https://openrouter.ai)) — this demo makes real LLM calls, nothing is stubbed

## Setup

```bash
cd backend
cp .env.example .env
# edit .env — set OPENROUTER_API_KEY to a real key

docker compose up -d postgres redis     # from the repo root
alembic upgrade head
python -m scripts.seed
```

`scripts/seed.py` is idempotent — safe to run again before a rehearsal.
It writes the canonical fixtures every tool call in this demo reads:
Rajesh Kumar's merchant row, Kumar General Store's wallet (₹18,450), the
`daily_txn` limit (₹25,000 limit, ₹24,890 already used — exactly ₹110 of
headroom left), and the declined ₹245 Amazon Business payment his call
opens with.

## Run it

```bash
python -m scripts.demo_cli --user usr_rajesh01
```

Asha speaks first — this is the whole "context-complete before the first
word" thesis the architecture is built around (docs/05 §1.1). You'll see
a greeting, then a `You:` prompt. Type `/end` (or Ctrl-D, or Ctrl-C) to
close the call — the CLI finalizes cost tracking, ends the session, and
prints a summary line with the total turn count, call cost, and session
id, which you can cross-reference directly in Postgres afterward.

## The canonical conversation

The scripted 9-turn incident this demo is built to reproduce
([docs/01-product-and-use-case.md](../docs/01-product-and-use-case.md)
§6-7, adapted for Phase 2's text-only, no-screen-context scope — see
below). Wording will vary since a real LLM is answering, not a script,
but the *structure* — which tools fire, the confirm-gate hold and
release, the final reference id — should match:

| Turn | You say (example) | What happens |
|---|---|---|
| 1 | *(the greeting — Asha speaks first)* | Profile-only greeting, no tool call |
| 2 | "My ₹245 payment to Amazon Business got declined, can you help?" | Acknowledgment, no tool call yet |
| 3 | "What happened? Do I have the money for it?" | `get_wallet_balance` + `get_payment_status` fire in parallel — Asha explains the wallet-vs-limit contradiction from real tool data |
| 4 | "Can you raise my daily limit?" | Sets up the request, no tool call yet |
| 5 | "Please request the maximum increase." | `request_limit_increase` is proposed — the confirm gate **holds**, Asha voices the action and asks for explicit confirmation |
| 6 | "Yes, do it." | Classified as an affirmation of the turn-5 proposal |
| 7 | "Go ahead, please." | The same call re-fires and **executes** — Asha reads back a real `LMT-####-####-####` reference id |
| 8 | "What happens next?" | Dialogue only |
| 9 | "That's all, thanks!" | Wrap-up, `/end` closes the call |

**What to watch for**, per the project's core correctness invariant (no
hallucinated account facts, docs/10-tool-calling.md §1): every ₹ amount
and reference id Asha speaks must trace to a real tool result from that
call. If you're following along in another terminal, `tool_invocations`
rows land in Postgres as each tool fires — you can query them live:

```sql
select turn_no, tool_name, status, idempotency_key from tool_invocations
  where session_id = '<the session id from the summary line>'
  order by turn_no;
```

This exact script — with scripted (not live) LLM responses, so it's
deterministic and Docker/API-key-free to run — is also what
[tests/e2e/test_canonical_conversation.py](tests/e2e/test_canonical_conversation.py)
replays and asserts against in CI: the confirm-gate hold at turn 5, the
idempotency-key switch from `:5` to `:7`, the exact tool arguments, and
the full post-call audit trail.

## Known limitations (Phase 2 scope, not bugs)

- **No screen context.** The real product's signature opener names the
  amount/payee/decline-reason *before the merchant speaks* by reading
  the screen (Phase 4). Phase 2 has no screen pipeline, so turn 1 is a
  profile-only greeting and turn 2 has you state the problem in words —
  see the Phase-2 plan's scope decision #2.
- **structlog JSON lines interleave with the transcript.** Both write to
  stdout; expect JSON log lines mixed into the `You:`/`Asha:` exchange,
  especially at `LOG_LEVEL=DEBUG`.
- **No Grafana trace at the end.** The roadmap's "ending on the Grafana
  trace for the turn" is a Phase 6 observability-stack milestone; Phase
  2's substitute is the printed session id plus the OpenTelemetry spans
  (`turn`, `tool.exec.<name>`) already being emitted to whatever
  `OTEL_EXPORTER_OTLP_ENDPOINT` is configured (or the console, if unset).
