# syntax=docker/dockerfile:1

# ---- base: runtime dependencies + app code -------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

WORKDIR /app
RUN useradd --create-home --uid 1000 app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY src/ src/
COPY prompts/ prompts/
COPY config.yaml pyproject.toml ./
RUN mkdir -p /app/data && chown app:app /app/data

# ---- test: base + pytest + tests (docker build --target test) -----------
FROM base AS test
COPY requirements-dev.txt .
RUN pip install -r requirements-dev.txt
COPY tests/ tests/
USER app
ENTRYPOINT ["python", "-m", "pytest", "-p", "no:cacheprovider"]
CMD ["-q"]

# ---- runtime: default target --------------------------------------------
FROM base AS runtime
USER app
VOLUME ["/app/data"]
ENTRYPOINT ["python", "-m", "yt_trends"]
CMD []
