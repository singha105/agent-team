# Sandbox runtime image

Build it before running any agent:

```bash
docker build -t agentteam-runtime:latest docker/
```

The image name is configurable via `AGENTTEAM_SANDBOX_IMAGE`.

Because the sandbox runs with `--network=none`, packages cannot be installed at
run time. Anything agents need must be baked into this image — see the `pip
install` layer in the Dockerfile, and keep it consistent with what the agent
system prompts in `config/agents/` tell agents is available.

To run without Docker, set `AGENTTEAM_SANDBOX_MODE=subprocess`. Read the
Sandboxing section of the top-level README first — that mode is not a security
boundary.
