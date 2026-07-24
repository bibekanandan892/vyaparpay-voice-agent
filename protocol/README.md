# protocol/ — Shared Contracts

Single source of truth for every contract shared between the Android client and the
backend. Both sides implement against these schemas; neither side owns them.

Planned contents (`schemas/`, JSON Schema):

- `screen_context.v1.json` — the semantic UI snapshot the app sends to the agent
- `app_event.v1.json` — user-action timeline events (taps, navigation, API errors)
- `data_channel_envelope.v1.json` — the LiveKit data-channel message envelope
- `tools/*.v1.json` — input/output contracts for every agent tool

Design rules:

- Every schema is versioned in its filename and in a `v` field on the wire.
- Additive changes only within a version; breaking changes bump the version.
- CI (Phase 2+) validates both the Kotlin serializers and the Pydantic models
  against these schemas.

Schemas land in Phase 2 alongside code — see [docs/17-roadmap.md](../docs/17-roadmap.md).
The wire formats are specified today in [docs/07-ui-semantic-context.md](../docs/07-ui-semantic-context.md)
and [docs/13-api-contracts.md](../docs/13-api-contracts.md).
