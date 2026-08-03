"""tests/providers-local fixtures.

`_fresh_structlog_config` (autouse): the provider modules log through
`app.obs.logging.get_logger`, and the wire tests here deliberately exercise
error paths that LOG (malformed frames, the fallback-voice retry). In
full-suite order, `tests/obs/test_logging.py` has already run
`configure_logging` — which installs `cache_logger_on_first_use=True` with
a `BytesLoggerFactory` bound to that test's (long-closed) pytest capture
stream — so a provider logger whose FIRST use happens in this directory
binds to a closed file and every `log.*` call explodes with
`ValueError: I/O operation on closed file`. (Observed concretely: these
tests pass in isolation and failed only in the full suite.) Configuring an
uncached `PrintLoggerFactory` per test makes every log call resolve the
CURRENTLY captured stdout instead — which is also what lets the
key-never-in-logs assertions read the log output via capsys —
and `reset_defaults()` afterwards leaves the library's safe defaults
rather than the closed-stream config. `structlog.configure` is a partial
update, so the processor chain whatever earlier test installed is left
alone; only the factory/caching keys that caused the crash are replaced.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import structlog


@pytest.fixture(autouse=True)
def _fresh_structlog_config() -> Iterator[None]:
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
    yield
    structlog.reset_defaults()
