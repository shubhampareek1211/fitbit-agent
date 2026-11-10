# Cloud Run image for Pulse (continuous deploy from GitHub builds this file).
# Local test: docker build -t pulse . && docker run -p 8080:8080 pulse
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.19 /uv /uvx /bin/

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never PYTHONUNBUFFERED=1

# Runtime dependencies only (no eval/test tools), in their own cached layer.
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-dev --no-install-project

# Only what the app needs at runtime: code, frontend, metric catalog and the committed DuckDB snapshot.
COPY app.py ./
COPY pulse ./pulse
COPY static ./static
COPY data/metrics.yaml ./data/metrics.yaml
COPY data/snapshots ./data/snapshots

ENV PATH="/app/.venv/bin:$PATH" PORT=8080
EXPOSE 8080
CMD ["python", "app.py"]
