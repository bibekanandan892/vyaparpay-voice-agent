"""Voice-pipeline components (docs/06-voice-pipeline.md) — the WebRTC
transport layer (PeerSession, SignalingServer, the `run` entrypoint) and the
worker-internal audio path between the peer and the agent brain: AudioIngress
(uplink fan-out), AudioEgress (paced playout + barge-in flush target), and
the sentence chunker (LLM text -> TTS dispatch units).

Import discipline (docs/06 §1.1): peer_session.py is the ONLY module allowed
to import aiortc; the other modules here touch at most `av` (PyAV) and the
frozen value types in app/domain/voice.py.
"""
