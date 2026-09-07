# Execution sandbox for agent commands.
#
# Every run_command invocation runs one command in a disposable container built
# from this image: --rm, --network=none, --read-only, --cap-drop ALL,
# no-new-privileges, with the workspace bind-mounted as the only writable path.
#
# Because there is no network at runtime, everything an agent might need has to
# be present here. `pip install` inside the sandbox will fail by design.

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        nodejs \
        npm \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Baked in because the sandbox has no network. Keep this list in step with what
# the agent system prompts claim is available.
RUN pip install --no-cache-dir \
        fastapi==0.141.1 \
        uvicorn==0.40.0 \
        pydantic==2.13.5 \
        pytest==9.0.1 \
        httpx==0.28.1 \
        sqlalchemy==2.0.52

# Containers run as the host uid:gid (passed with --user at run time) so files
# created in the bind-mounted workspace are owned by the host user rather than
# root. That uid has no passwd entry, so give it a writable HOME on tmpfs.
ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GIT_CONFIG_GLOBAL=/tmp/.gitconfig

WORKDIR /workspace
