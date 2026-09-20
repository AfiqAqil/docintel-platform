# syntax=docker/dockerfile:1

# Multi stage, so the build tooling never reaches the runtime image.
# Targets arm64, matching the ARM64 runtime platform in the ECS task definitions and the
# arm64 CI runner, so nothing is ever built under emulation.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build

# Dependencies are installed from the project metadata alone, before any application code is
# copied, so a code change does not invalidate the dependency layer. This is the single
# biggest thing separating a 5 second rebuild from a 3 minute one.
COPY pyproject.toml ./
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
 && /opt/venv/bin/pip install .

COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./
RUN /opt/venv/bin/pip install --no-deps .


FROM python:3.12-slim AS runtime

# curl is for the container health check, which runs inside the task because the API has no
# target group: nothing outside the VPC can reach it to check it.
RUN apt-get update \
 && apt-get install --no-install-recommends -y curl \
 && rm -rf /var/lib/apt/lists/*

# Non root. Nothing in this service needs to write outside its own process.
RUN useradd --create-home --uid 10001 appuser

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=appuser:appuser app ./app
COPY --chown=appuser:appuser alembic ./alembic
COPY --chown=appuser:appuser alembic.ini ./

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
