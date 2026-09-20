"""Shared test setup.

Everything here exists so the graph can be driven end to end with no network, no AWS and no
database. The fake chat model is a real BaseChatModel installed through the same provider
seam production uses, so these tests exercise the real wiring rather than monkeypatching
node internals and proving nothing.
"""

from __future__ import annotations

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
