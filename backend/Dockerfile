# backend/Dockerfile — agent-api runtime image.
#
# Builds the FastAPI service defined by backend/pyproject.toml. app.main:app
# doesn't exist yet as of this file's authoring (Batch 1 of the Phase-2
# plan) — it lands in a later batch — but the CMD is wired correctly now so
# no later task needs to touch this file.

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

RUN pip install --upgrade pip \
    && pip install .

# Run as a non-root user.
RUN useradd --create-home --shell /usr/sbin/nologin agent \
    && chown -R agent:agent /app
USER agent

EXPOSE 8000

# Apply pending Alembic migrations, then start the API server.
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000"]
