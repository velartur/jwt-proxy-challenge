# syntax=docker/dockerfile:1

# ---------------------------------------------------------------------------
# base: the application and its runtime dependencies, nothing else
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

# ---------------------------------------------------------------------------
# test: base + test dependencies + the test suite (never shipped)
# ---------------------------------------------------------------------------
FROM base AS test

RUN pip install ".[test]"
COPY tests ./tests
CMD ["pytest", "-q"]

# ---------------------------------------------------------------------------
# runtime: what `make run` executes
# ---------------------------------------------------------------------------
FROM base AS runtime

# Inside the container the proxy must listen on all interfaces so the published
# port can reach it; exposure is controlled by the port mapping in
# docker-compose.yml, which defaults to 127.0.0.1 on the host.
ENV HTTP_HOST=0.0.0.0 \
    HTTP_PORT=8080

RUN useradd --system --uid 10001 --no-create-home jwtproxy  # "proxy" already exists in Debian
USER jwtproxy

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import os, sys, urllib.request; \
        r = urllib.request.urlopen('http://127.0.0.1:%s/status' % os.environ['HTTP_PORT'], timeout=2); \
        sys.exit(0 if r.status == 200 else 1)"

CMD ["python", "-m", "jwt_proxy"]
