# syntax=docker/dockerfile:1

# The worker is the heaviest of the three images: it parses PDFs, renders pages to images
# and calls a model. It gets more CPU and memory in the task definition for the same reason.
FROM python:3.12-slim AS builder

# uv is used in the builder only, to install exactly what uv.lock pins. Two builds of the same
# commit therefore ship the same library versions, which a range in pyproject.toml alone
# does not guarantee. The runtime stage never sees uv.
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /bin/uv

ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /build

# Dependencies only, in their own layer, so a code change does not reinstall them.
# --frozen fails the build if uv.lock is out of step with pyproject.toml.
# The application itself is not installed as a package: the runtime stage copies the
# source into /app and runs it from there.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project


FROM python:3.12-slim AS runtime

RUN useradd --create-home --uid 10001 appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HEARTBEAT_FILE=/tmp/worker-heartbeat

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser consumer ./consumer
COPY --chown=appuser:appuser graph ./graph
COPY --chown=appuser:appuser llm ./llm
COPY --chown=appuser:appuser rules ./rules
COPY --chown=appuser:appuser config.py ./

USER appuser

# The worker has no listener, so there is no port to probe. The poll loop writes a timestamp
# on every iteration and this checks it is recent: a process liveness check would pass for a
# wedged loop, which is exactly the failure worth catching.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD python -c "import os,sys,time; f=os.environ['HEARTBEAT_FILE']; sys.exit(0 if time.time()-float(open(f).read()) < 120 else 1)" || exit 1

# No ENTRYPOINT wrapper: the process must be PID 1 so that SIGTERM from ECS reaches it
# directly. The consumer traps it, stops polling and finishes the message in flight.
CMD ["python", "-m", "consumer.main"]
