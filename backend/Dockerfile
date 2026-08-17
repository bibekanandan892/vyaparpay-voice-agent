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

# The Silero VAD model is baked into the image here, in a layer keyed on
# the fetch script alone (so it is re-downloaded only when the pin/hash in
# that script changes, never on an ordinary source edit).
#
# HIGH fix (verification audit, 2026-08-17): the model file is gitignored
# (`backend/app/voice/models/*`) and, until this step existed, nothing in
# this Dockerfile ever fetched it — `COPY backend/ /app/` below only ships
# it if the HOST had already run `python -m scripts.fetch_models` before
# `docker compose build`. The README ordered that fetch AFTER
# `docker compose up`, so an image built by following the docs had no
# model, and voice-worker failed every call at peer setup
# (`SileroVad.__init__` raises on the missing file). On the dev machine it
# only ever worked because the model had been fetched into the running
# container by hand — which a rebuild or `compose down` silently undid.
#
# The script is stdlib-only, pins a release tag + SHA-256, refuses
# non-https, and skips the network entirely when the file is already
# present and verified — so this layer is deterministic and cheap. It is
# run from a scratch location so its `Path(__file__)`-relative destination
# resolves under /tmp/model, and the verified file is copied into place
# after the source COPY (a COPY merges into an existing directory, but
# an explicit `cp` is unambiguous about which one wins).
COPY backend/scripts/fetch_models.py /tmp/model/scripts/fetch_models.py
RUN python /tmp/model/scripts/fetch_models.py

# Full source is copied before `pip install .` because the build backend
# (hatchling, see pyproject.toml) needs the `app/` package present to
# resolve `packages = ["app"]` — a pyproject-only layer would fail to
# build. This trades away a dependency-only cache layer for correctness
# and simplicity; revisit only if build times become a real problem.
COPY backend/ /app/

# `protocol/` is a SIBLING of `backend/` in the repo, not inside it —
# schema_validation.py's load_schema() resolves it as
# `Path(__file__).resolve().parents[3] / "protocol" / "schemas"`, which
# from this image's `/app/app/context/schema_validation.py` is
# `/protocol/schemas`. HIGH fix, found on the first real device call
# (2026-08-07): the build context used to be `./backend` (docker-compose
# .yml), so `protocol/` was never reachable by any COPY in this file —
# every mid-call `app_event.v1` screen update failed validation with
# FileNotFoundError and was silently dropped, for the whole time this
# service has existed. The build context is now the repo root (see
# docker-compose.yml's `context: .` / `dockerfile: backend/Dockerfile`),
# so this COPY can actually see it. Root `.dockerignore` carries forward
# the secret-exclusion rules `backend/.dockerignore` used to own alone.
COPY protocol/ /protocol/

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

# Place the pre-fetched, hash-verified model where app/voice/silero.py's
# SILERO_MODEL_PATH resolves it (`<silero.py dir>/models/silero_vad.onnx`).
# AFTER `pip install` on purpose: hatchling packages everything under
# app/, so a model already sitting there would also be baked into the
# installed wheel — a harmless but pointless 2.3 MB duplicate (both
# entrypoints import the /app source tree, not site-packages). `mkdir -p`
# because the gitignored models/ dir may arrive from the host with only
# its .gitkeep — or, under a stricter .dockerignore, not at all.
RUN mkdir -p /app/app/voice/models \
    && cp /tmp/model/app/voice/models/silero_vad.onnx /app/app/voice/models/silero_vad.onnx \
    && rm -rf /tmp/model

# Run as a non-root user.
RUN useradd --create-home --shell /usr/sbin/nologin agent \
    && chown -R agent:agent /app
USER agent

EXPOSE 8000

# Apply pending Alembic migrations, then start the API server.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
