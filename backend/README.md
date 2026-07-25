# backend/ — Agent Backend (Python / FastAPI)

FastAPI service (`agent-api`) plus a raw-WebRTC voice worker (`voice-worker`) —
signaling WebSocket, aiortc peers, and the hand-rolled voice pipeline — that
hosts the AI support agent. Architecture: [docs/04-backend-architecture.md](../docs/04-backend-architecture.md)
and [docs/05-agent-architecture.md](../docs/05-agent-architecture.md).

Planned package map:

```
app/
├── api/          # FastAPI routers: sessions, context, business APIs (seeded demo data)
├── agent/        # SessionManager, ConversationManager, PromptBuilder, ContextBuilder,
│                 #   ToolExecutor, LLMRouter, SafetyLayer, CostTracker, Summarizer
├── context/      # SnapshotIngestor, EventLog, ContextCompressor
├── memory/       # ShortTermMemory, SessionMemory, UserProfileMemory, SemanticMemory
├── tools/        # tool registry + one module per business tool
├── providers/    # LLMProvider (OpenRouter), SttProvider (Deepgram),
│                 #   TtsProvider (ElevenLabs), EmbeddingProvider
├── voice/        # SignalingServer, PeerSession (aiortc), AudioIngress,
│                 #   VadEndpointer, AudioEgress, VoiceAgentWorker
└── models/       # Pydantic schemas + SQLAlchemy ORM
tests/
```

Code lands in Phase 2 — see [docs/17-roadmap.md](../docs/17-roadmap.md).
