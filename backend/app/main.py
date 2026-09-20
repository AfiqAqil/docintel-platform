"""The FastAPI app.

Migrations run once at startup, before the app accepts traffic (ARCHITECTURE section 10).
`/healthz` is defined here rather than in the documents router because it must never depend on
`get_db`: it backs the ECS container health check, and a check that fails when the database
blips would cause ECS to kill tasks that are otherwise fine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.db import run_migrations
from app.logging import configure_logging
from app.routers import documents

configure_logging()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    run_migrations()
    yield


app = FastAPI(title="docintel-backend", lifespan=lifespan)
app.include_router(documents.router, prefix="/api/documents", tags=["documents"])


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}
