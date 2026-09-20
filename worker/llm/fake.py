"""A fake chat model, so the whole graph can be exercised with no network call.

This is what CI runs against. It is a real BaseChatModel, so it goes through the same
`with_structured_output` path the production code uses, rather than the tests monkeypatching
node internals and proving nothing about the wiring.

A script is a list of responses, returned in order. A response is either a Pydantic model
instance, which structured output returns as is, or an exception instance, which is raised.
Raising is how tests drive the transient and terminal error routes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.runnables import Runnable, RunnableLambda


class FakeChatModel(BaseChatModel):
    """Returns queued responses in order. Records every call for assertions."""

    responses: list[Any] = []
    calls: list[list[BaseMessage]] = []

    def __init__(self, responses: Sequence[Any] | None = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        # Copy, so a test's list is not consumed out from under it.
        self.responses = list(responses or [])
        self.calls = []

    @property
    def _llm_type(self) -> str:
        return "fake"

    def _next(self, messages: list[BaseMessage]) -> Any:
        self.calls.append(list(messages))
        if not self.responses:
            raise AssertionError(
                f"FakeChatModel ran out of scripted responses after {len(self.calls)} calls"
            )
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def _generate(self, messages: list[BaseMessage], **kwargs: Any) -> ChatResult:
        response = self._next(messages)
        text = response if isinstance(response, str) else str(response)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text))])

    def with_structured_output(self, schema: Any, **kwargs: Any) -> Runnable:
        """Return the queued object directly, bypassing parsing.

        Production goes model -> JSON -> Pydantic. Here the queued object is already the
        Pydantic instance, so there is nothing to parse. What this still exercises is that
        the node asked for the right schema and handled the object it got back.
        """

        def _invoke(messages: Any) -> Any:
            normalised = messages if isinstance(messages, list) else [messages]
            return self._next(normalised)

        return RunnableLambda(_invoke)
