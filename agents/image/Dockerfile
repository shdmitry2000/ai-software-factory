# Agent image: Python agents + pinned Claude Code. Build context = the factory repo root.
#   docker build -f agents/image/Dockerfile -t agent .
# No credentials inside: the runtime mounts the LLM credential at run time
# (FACTORY_ENV_FILE); Google access comes from the host's service account.
FROM node:22-bookworm-slim

ARG CLAUDE_CODE_VERSION=2.1.310
ENV DEBIAN_FRONTEND=noninteractive \
    DISABLE_AUTOUPDATER=1 \
    CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1 \
    PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-venv ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && python3 -m venv /opt/venv

WORKDIR /app
COPY mission-control/pyproject.toml mission-control/pyproject.toml
COPY mission-control/mission_control mission-control/mission_control
COPY mission-control/schema mission-control/schema
RUN pip install --no-cache-dir "./mission-control[pubsub,firestore]"
COPY agents agents
COPY products products
# prompts/ is baked in: a rules change = redeploy (bin/agents/deploy.sh rebuilds in seconds)
COPY prompts prompts

RUN useradd --uid 10001 --create-home agent
USER 10001
ENV HOME=/home/agent PYTHONPATH=/app
ENTRYPOINT ["python", "-m", "agents.compliance"]
CMD ["--bus", "pubsub", "--store", "firestore", "--interval", "600"]
