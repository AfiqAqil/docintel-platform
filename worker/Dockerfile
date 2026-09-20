# syntax=docker/dockerfile:1

# The worker is the heaviest of the three images: it parses PDFs, renders pages to images
# and calls a model. It gets more CPU and memory in the task definition for the same reason.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .

COPY consumer ./consumer
COPY graph ./graph
COPY llm ./llm
COPY rules ./rules
COPY config.py ./
RUN /opt/venv/bin/pip install --no-deps .


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
