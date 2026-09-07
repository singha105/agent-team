# The AgentTeam API itself. This is NOT the agent sandbox — agent commands run
# in disposable containers built from docker/Dockerfile.

FROM python:3.12-slim

# The API shells out to `docker run` to create sandbox containers, so it needs
# the Docker CLI. It talks to the host daemon over the mounted socket; no
# daemon runs inside this image.
RUN apt-get update && apt-get install -y --no-install-recommends \
        docker.io \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv

COPY pyproject.toml README.md ./
COPY backend ./backend
RUN pip install --no-cache-dir -e .

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
