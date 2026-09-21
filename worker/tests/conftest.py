"""Shared test setup.

Everything here exists so the graph can be driven end to end with no network, no AWS and no
database. The fake chat model is a real BaseChatModel installed through the same provider
seam production uses, so these tests exercise the real wiring rather than monkeypatching
node internals and proving nothing.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.fake import FakeChatModel  # noqa: E402
from llm.provider import set_override  # noqa: E402


@pytest.fixture
def fake_llm():
    """Install a fake chat model and remove it afterwards.

    The test scripts the responses; running out of scripted responses is an assertion
    failure, so a graph that makes an unexpected extra model call fails loudly rather than
    silently passing.
    """
    created: list[FakeChatModel] = []

    def _install(responses):
        model = FakeChatModel(responses)
        created.append(model)
        set_override(model)
        return model

    yield _install
    set_override(None)



DATABASE_DSN = os.environ.get(
    "TEST_DATABASE_DSN", "postgresql://docintel:docintel@localhost:55432/docintel"
)


@pytest.fixture(scope="session")
def database_schema() -> None:
    """Create the schema with the backend's migration, never with a copy of the DDL.

    Every database test depends on this, through its own `conn` fixture. It used to live in
    one test module, which meant the other module only passed when something else had already
    created the table: locally the backend's tests had, and in CI, where each service gets its
    own empty database, nothing had. Not autouse, so the graph tests still need no database.

    If alembic is missing the tests skip rather than quietly creating the table here, because
    a second definition of the schema is exactly what this is designed to prevent. CI fails
    the build on any skip, so that cannot pass silently either.
    """
    backend = Path(__file__).resolve().parents[2] / "backend"
    alembic = backend / ".venv" / "bin" / "alembic"
    if not alembic.exists():
        pytest.skip(f"backend virtualenv not built at {alembic}")

    subprocess.run(
        [str(alembic), "upgrade", "head"],
        cwd=backend,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "DATABASE_URL": DATABASE_DSN.replace("postgresql://", "postgresql+psycopg://"),
        },
    )
