"""Voice-pipeline components (docs/06-voice-pipeline.md) — the worker-internal
audio path between the aiortc peer (PeerSession, a later task) and the agent
brain: AudioIngress (uplink fan-out), AudioEgress (paced playout + barge-in
flush target), and the sentence chunker (LLM text -> TTS dispatch units).

Import discipline (docs/06 §1.1): PeerSession is the only class allowed to
import aiortc; the modules here touch at most `av` (PyAV) and the frozen
value types in app/domain/voice.py.
"""
