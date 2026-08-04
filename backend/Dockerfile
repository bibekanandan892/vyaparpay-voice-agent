# backend/Dockerfile — the SHARED runtime image for both backend services.
#
# docs/04 §1 is "one image, built once, started two ways": docker-compose.yml
# builds this once as `vyapar-backend:dev` and runs it as both `agent-api`
# (this file's CMD) and `voice-worker` (which overrides CMD with
# `python -m app.voice.run`). Calling it "the agent-api image" — as this
# comment did until 2026-08-04 — is what made it easy to miss that the
# install line below has to satisfy the worker's imports too; see that line.
#
# Builds the FastAPI service defined by backend/pyproject.toml. (An earlier
# version of this comment said `app.main:app` "doesn't exist yet" — it was
# written in Phase-2 Batch 1, ahead of the code, and has been stale since
# that module landed.)

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Full source is copied before `pip install .` because the build backend
# (hatchling, see pyproject.toml) needs the `app/` package present to
# resolve `packages = ["app"]` — a pyproject-only layer would fail to
# build. This trades away a dependency-only cache layer for correctness
# and simplicity; revisit only if build times become a real problem.
COPY . .

# The `[voice]` extra is NOT optional here, despite only one of the two
# services needing it. docs/04 §1 is "one image, built once, started two
# ways" and docker-compose.yml builds exactly one image (`vyapar-backend:dev`)
# for both `agent-api` and `voice-worker`, which differ only in `command:`.
# `voice-worker` runs `python -m app.voice.run`, whose import graph reaches
# aiortc/deepgram/elevenlabs — all declared in the `voice` extra, none in the
# base install. Audit fix (2026-08-04): this was a plain `pip install .`, so
# the voice-worker container could not import its own entrypoint at all — it
# would have died on `ModuleNotFoundError` the first time anyone ran the
# stack with a real worker. Installing the extra into the shared image costs
# agent-api a slightly larger layer and buys a worker that actually starts;
# splitting into two images (or a multi-stage target) is the alternative, and
# is the right move only if that layer size ever becomes a real problem —
# it would trade away the single-build property compose relies on today.
RUN pip install --upgrade pip \
    && pip install ".[voice]"

# Run as a non-root user.
RUN useradd --create-home --shell /usr/sbin/nologin agent \
    && chown -R agent:agent /app
USER agent

EXPOSE 8000

# Apply pending Alembic migrations, then start the API server.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
