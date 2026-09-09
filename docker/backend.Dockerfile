# The AgentTeam API.
#
# This is NOT the agent sandbox — agent commands run in disposable containers
# built from docker/Dockerfile. This image runs the FastAPI app, the worker
# pool and the event bus.

# ---- build -----------------------------------------------------------------
# A separate stage so build tooling and wheel caches never reach the runtime
# image. Anything only needed to install dependencies stays here.
FROM python:3.12-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependency metadata first, so a source change does not invalidate the
# dependency layer on every rebuild.
COPY pyproject.toml README.md ./
COPY backend/app/__init__.py ./backend/app/__init__.py

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN pip install --no-cache-dir .

# ---- runtime ---------------------------------------------------------------
FROM python:3.12-slim AS runtime

# The docker CLI only: the API shells out to `docker run` to create sandbox
# containers and talks to the host daemon over a mounted socket. No daemon runs
# in this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Non-root. The uid is fixed so a bind-mounted workspace has predictable
# ownership, and compose can override it to match the host user.
ARG UID=10001
ARG GID=10001
RUN groupadd --gid "${GID}" agentteam \
    && useradd --uid "${UID}" --gid "${GID}" --create-home --shell /usr/sbin/nologin agentteam

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/srv/backend

WORKDIR /srv
COPY --chown=agentteam:agentteam pyproject.toml README.md ./
COPY --chown=agentteam:agentteam backend ./backend
COPY --chown=agentteam:agentteam config ./config
COPY --chown=agentteam:agentteam scripts ./scripts

# Writable at runtime; also created here so the container works without a bind
# mount, which is what makes `docker run` alone usable for a smoke test.
RUN mkdir -p /srv/data /srv/workspace && chown -R agentteam:agentteam /srv/data /srv/workspace

USER agentteam

EXPOSE 8000

# Hits the app's own readiness check, which reports 503 when the database is
# unreachable — so an unhealthy container is one that genuinely cannot serve,
# not merely one whose port is open.
HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD curl --fail --silent http://127.0.0.1:8000/health || exit 1

CMD ["sh", "-c", "alembic -c backend/alembic.ini upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000"]
