# syntax=docker/dockerfile:1

# ---------- Stage 1: build the virtual environment ----------
FROM python:3.13-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# README.md and LICENSE are referenced by pyproject.toml
COPY pyproject.toml README.md LICENSE ./
COPY config ./config
COPY src ./src

RUN pip install .

# ---------- Stage 2: runtime ----------
FROM python:3.13-slim AS runtime

# Container defaults for the settings. Everything else comes from the environment
# (docker compose env_file / environment); no .env file is baked into the image.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8000

RUN useradd --create-home --uid 1000 appuser

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY main.py ./

USER appuser

EXPOSE 8000

# The application writes only to stdout/stderr, so the filesystem can stay read-only.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import os, urllib.request as u; u.urlopen('http://127.0.0.1:%s/health/live' % os.environ['HTTP_PORT'], timeout=2)" || exit 1

CMD ["python", "main.py"]
