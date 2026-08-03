# infra/ — Local Infrastructure

Everything needed to run the platform locally, declarative and versioned
next to the code it runs. Design context:
[docs/02-system-architecture.md](../docs/02-system-architecture.md) §6 and
[docs/16-tech-stack.md](../docs/16-tech-stack.md) (ADR-006 for coturn).

The compose file itself lives at the repo root
([docker-compose.yml](../docker-compose.yml)) so `docker compose up` works
from a fresh clone with no `-f` flag; this directory holds the config files
it mounts.

```
infra/
└── docker/
    ├── coturn/
    │   ├── turnserver.conf     # coturn config — every directive commented
    │   ├── gen-cert.sh         # dev self-signed cert for the turns: listener
    │   └── certs/              # gen-cert.sh output (*.pem, gitignored)
    ├── tempo/tempo.yaml        # minimal trace sink (obs profile)
    └── grafana/provisioning/   # Tempo datasource, pre-wired (obs profile)
```

---

## Bring the stack up

```bash
# 1. secrets — every var is documented in the template
cp backend/.env.example backend/.env
$EDITOR backend/.env            # fill provider keys + TURN_SECRET (see below)

# 2. the TLS cert coturn's turns: listener needs — once, idempotent
./infra/docker/coturn/gen-cert.sh

# 3. up
docker compose up -d postgres redis coturn agent-api
#    ^ voice-worker is deliberately omitted here — see the note below

# 4. optional: trace sink
docker compose --profile obs up -d
```

**Why step 3 names services instead of a bare `up`:** `voice-worker` runs
`python -m app.voice.run`, and **`backend/app/voice/run.py` does not exist
yet** — it lands in a later Phase-3 task. The service is wired ahead of the
code on purpose (the infrastructure shape is reviewable now, and that task
then adds only `run.py`), but until it lands a plain `docker compose up`
brings everything else up and leaves that one container exiting with
`No module named app.voice.run`.

A second thing that task will need: the image installs `pip install .`
(base dependencies only), while the worker needs the `voice` extra
(aiortc, onnxruntime, av, numpy — `backend/pyproject.toml`). Whoever wires
`run.py` owns that Dockerfile change; it is not done here because this task
does not touch `backend/`.

---

## What each service is for

| Service | Image | Purpose | Ports (host) |
|---|---|---|---|
| `postgres` | `pgvector/pgvector:pg16` | Business data, conversations, embeddings | `127.0.0.1:5432` |
| `redis` | `redis:7` | Session state, signaling tokens, rate limiting | `127.0.0.1:6379` |
| `coturn` | `coturn/coturn:4.16` | STUN + TURN relay — NAT traversal for the two WebRTC peers (ADR-006) | `3478/udp+tcp`, `5349/tcp+udp`, `49160-49200/udp` |
| `agent-api` | `./backend` | FastAPI: session mint, business APIs, migrations | `8000` |
| `voice-worker` | `./backend` (same image) | `/v1/signal` WebSocket, aiortc peer, voice pipeline | `8080` |
| `tempo` | `grafana/tempo:2.9.4` | OTLP trace sink — `obs` profile only | `127.0.0.1:3200`, `127.0.0.1:4317` |
| `grafana` | `grafana/grafana:13.1` | Trace viewer — `obs` profile only | `127.0.0.1:3000` |

`agent-api` and `voice-worker` are the same image (`vyapar-backend:dev`)
started two ways — [docs/04 §1](../docs/04-backend-architecture.md). They
differ in `command:` and in `OTEL_SERVICE_NAME`, nothing else.

Note which ports are loopback-bound and which are not. The datastores and
the observability UIs are `127.0.0.1`-only because they have no
authentication worth the name. `agent-api` (8000), `voice-worker` (8080)
and coturn are bound on all interfaces because a phone has to reach them.

---

## How coturn gets its secret

coturn's config parser has **no environment-variable interpolation** — a
`static-auth-secret=${TURN_SECRET}` line in `turnserver.conf` would
configure the secret to those literal characters. Since canon forbids a
hardcoded secret in a tracked file, the compose service's entrypoint
renders the config at container start:

1. copy the tracked `turnserver.conf` to `/tmp/turnserver.conf` under
   `umask 077`,
2. append `static-auth-secret=$TURN_SECRET` from the container environment
   (which comes from `backend/.env`),
3. append `external-ip=$TURN_EXTERNAL_IP` **if** that variable is set,
4. `exec turnserver -c /tmp/turnserver.conf`.

Passing `--static-auth-secret` on the command line would also work and is
what most examples do; appending to a 0600 temp file instead keeps the
secret out of the container's process table as well as out of git.

The same `TURN_SECRET` is what agent-api HMACs `ice_servers` credentials
with ([docs/13 §7](../docs/13-api-contracts.md)) — the two must be the
identical value, which is why both containers read the same
`backend/.env`. The entrypoint fails fast with an actionable message if
`TURN_SECRET` is empty or the TLS cert is missing, rather than starting a
coturn that rejects every allocation.

Generate a secret with something like `openssl rand -hex 32`. Keep it
alphanumeric — it passes through a shell in the entrypoint, and a value
containing quotes or backticks is asking for trouble.

---

## What works locally, and what needs real values

| Thing | On a laptop | Needs a real value when |
|---|---|---|
| `TURN_SECRET` | Any random string works — both sides just have to agree | Always. There is no default; empty means coturn rejects every allocation. |
| `COTURN_HOST` | Fine as `localhost` **only** for host-local tests | A phone is involved — it must be the host's LAN IP or a DNS name the phone resolves |
| `SIGNALING_PUBLIC_URL` | `ws://localhost:8080/v1/signal` | A phone is involved — same rule; and production requires `wss://` |
| coturn TLS cert | `gen-cert.sh` self-signed is enough for local `turns:` experiments | Android is the client — it rejects an untrusted chain, so `turns:` silently never gets used |
| Postgres / Redis credentials | Dev defaults baked into compose | Anything reachable from outside the host |
| Provider keys (Deepgram, ElevenLabs, OpenRouter) | Real keys required — there are no local stand-ins | Always |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | Ships empty (export disabled); set to `http://tempo:4317` for the `obs` profile | — |

---

## The phone has to reach three things

This is the failure mode that costs the most time, so it gets its own
heading. Testing on a real device means the handset must reach, **on
whatever host runs this stack**:

1. **agent-api** — `http://<host>:8000` — to mint the session.
2. **the voice-worker signalling WS** — `ws://<host>:8080/v1/signal` — and
   the URL the app dials is whatever `SIGNALING_PUBLIC_URL` says, not what
   compose published. If those disagree, the session mints and the
   WebSocket never connects.
3. **coturn** — `<host>:3478` UDP/TCP plus the `49160-49200/udp` relay
   range — and the URL the app dials comes from `COTURN_HOST`, same trap.

`localhost` in `backend/.env` satisfies none of these from a phone. Set
`COTURN_HOST` and `SIGNALING_PUBLIC_URL` to the host's LAN IP (or an ngrok
hostname for the two HTTP/WS surfaces — media cannot tunnel and must go to
the LAN IP directly, [docs/04 §9](../docs/04-backend-architecture.md)),
and make sure the host firewall is not eating UDP.

---

## Before this is a real deployment

Five things in here are demo shortcuts, and each is deliberate. None
should survive contact with a public network:

- **The TLS cert is self-signed.** `gen-cert.sh` says so at the top.
  Replace both `certs/*.pem` files with a CA-issued cert for the real
  `COTURN_HOST` name, or Android will never use the `turns:` fallback the
  restrictive-network path depends on.
- **coturn does not deny private peer ranges.** The standard hardening is
  `denied-peer-ip=` for the RFC1918 blocks, so the relay cannot be pointed
  at internal services. It is off here because the legitimate peer — the
  voice-worker container — *is* on a private compose address. The moment
  the worker is not a 172.x neighbour, turn those lines on (they are
  written out, commented, in `turnserver.conf`).
- **coturn's relay candidates carry the container's IP.** On the bridge
  network coturn believes it lives at 172.x, which no phone can route to.
  Set `TURN_EXTERNAL_IP` to the host's reachable address (the entrypoint
  appends it as coturn's `external-ip`), or run coturn with
  `network_mode: host` — Linux only, and it bypasses the published-port
  mapping entirely.
- **The voice-worker's media ports are not published.** aiortc allocates
  ephemeral UDP ports that compose cannot pin, so on the bridge network the
  worker's host candidates are unroutable from a phone and calls fall back
  to the coturn relay. `network_mode: host` (Linux) is the fix if you want
  the direct path; the honest demo answer is that the relay carries it.
- **`backend/.env` goes into every container**, including coturn, which
  only needs `TURN_SECRET`. One secrets file is what guarantees agent-api
  and coturn agree on that secret; the cost is a wider blast radius for the
  provider keys. Production passes each service only what it needs.

Also: the `obs` profile runs Tempo as root (a fresh named volume is
root-owned and Tempo's uid 10001 cannot write its WAL) and Grafana with
anonymous admin and no login form. Both are fine for a loopback-bound local
UI and unacceptable anywhere else.

---

## Validation

CI runs `docker compose config -q` on every PR (the `docker-gated` job in
[.github/workflows/ci.yml](../.github/workflows/ci.yml)), which catches
compose syntax, interpolation, and schema errors. Run the same check
locally before pushing:

```bash
docker compose config -q
```

It does **not** validate `turnserver.conf` or `tempo.yaml` — those are only
exercised by actually starting the containers.
