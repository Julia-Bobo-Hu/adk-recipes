FROM python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PATH="/app/.venv/bin:${PATH}"

# Install the versions pinned in uv.lock, including the mcp extra so the same container can
# run either the workstation (scripts/server.py) or the Streamable HTTP MCP App (scripts/mcp_server.py).
COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir "uv>=0.5" \
    && uv sync --frozen --no-dev --extra mcp --no-install-project

COPY README.md EVAL.yaml SKILL.md ./
COPY scripts/ ./scripts/
COPY references/ ./references/
COPY assets/ ./assets/

EXPOSE 8080

# The sign-off log (_signoff/) and saved evaluation runs (_output/) are written inside the
# container. They are lost when the instance stops.
CMD ["python3", "scripts/server.py", "--host", "0.0.0.0", "--port", "8080"]
