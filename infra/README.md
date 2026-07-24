# infra/ — Local Infrastructure

Docker Compose stack for running the whole system locally. Design:
[docs/02-system-architecture.md](../docs/02-system-architecture.md) and
[docs/16-tech-stack.md](../docs/16-tech-stack.md).

Planned compose services:

| Service    | Image                  | Purpose                                    |
|------------|------------------------|--------------------------------------------|
| livekit    | livekit/livekit-server | WebRTC SFU + data channels                 |
| postgres   | pgvector/pgvector      | Business data, conversations, embeddings   |
| redis      | redis                  | Session state, memory cache, rate limiting |
| agent-api  | ./backend              | FastAPI service                            |
| voice-worker | ./backend            | LiveKit Agents worker (the AI agent)       |
| grafana    | grafana/grafana        | Latency/cost dashboard                     |
| tempo      | grafana/tempo          | OpenTelemetry trace storage                |

Compose files land in Phase 2 — see [docs/17-roadmap.md](../docs/17-roadmap.md).
