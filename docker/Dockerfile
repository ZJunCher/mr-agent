FROM python:3.12.10-slim AS base

RUN rm -rf /var/lib/apt/lists/* && apt-get update && apt-get install --no-install-recommends -y \
        git curl ca-certificates xz-utils && apt-get clean && rm -rf /var/lib/apt/lists/*

# Copilot CLI (legacy, kept for fallback)
# RUN curl -fsSL https://gh.io/copilot-install | bash

# Hermes CLI (coding agent, replaces Copilot CLI)
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh \
    | bash -s -- \
        --skip-setup \
        --skip-browser \
        --non-interactive
RUN command -v hermes && hermes --help || echo "hermes install check (may need PATH refresh)"

WORKDIR /app
ADD pyproject.toml .
ADD requirements.txt .
ADD docs docs
RUN pip install --no-cache-dir . && rm pyproject.toml requirements.txt
ENV PYTHONPATH=/app

FROM base AS github_app
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py", "--forwarded-allow-ips", "*", "pr_agent.servers.github_app:app"]

FROM base AS bitbucket_app
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "pr_agent/servers/bitbucket_app.py"]

FROM base AS bitbucket_server_webhook
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "pr_agent/servers/bitbucket_server_webhook.py"]

FROM base AS github_polling
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "pr_agent/servers/github_polling.py"]

FROM base AS gitlab_runtime
ADD pr_agent pr_agent
ADD ut_agent ut_agent
RUN pip install --no-cache-dir lark-oapi && mkdir -p /app/ut_agent/workspace/logs
# Model and Git provider credentials must be injected at runtime.

FROM gitlab_runtime AS gitlab_webhook
CMD ["python", "pr_agent/servers/gitlab_webhook.py"]

FROM gitlab_runtime AS gitlab_web
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-w", "2", "-b", "0.0.0.0:3000", "pr_agent.servers.gitlab_webhook:app"]

FROM gitlab_runtime AS gitlab_agent
CMD ["python", "-m", "pr_agent.distributed.agent_worker"]

FROM gitlab_runtime AS feishu_worker
CMD ["python", "-m", "pr_agent.servers.feishu_worker"]

FROM base AS azure_devops_webhook
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "pr_agent/servers/azuredevops_server_webhook.py"]

FROM base AS gitea_app
ADD pr_agent pr_agent
ADD ut_agent ut_agent
CMD ["python", "-m", "gunicorn", "-k", "uvicorn.workers.UvicornWorker", "-c", "pr_agent/servers/gunicorn_config.py","pr_agent.servers.gitea_app:app"]


FROM base AS test
ADD requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt && rm requirements-dev.txt
ADD pr_agent pr_agent
ADD ut_agent ut_agent
ADD tests tests

FROM base AS cli
ADD pr_agent pr_agent
ADD ut_agent ut_agent
ENTRYPOINT ["python", "pr_agent/cli.py"]
