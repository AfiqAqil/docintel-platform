"""Chat model construction, and the one place the provider is chosen.

The graph never imports a provider SDK. It asks for a model here, and this module decides
whether that is Bedrock or OpenAI based on configuration. That boundary is what lets the
whole graph run in tests against a fake, with no network and no credentials.

Default mode is Bedrock with IAM authentication and no stored credential. The OpenAI
fallback exists for when the account holds no Bedrock token quota; it reads its key from
Secrets Manager at startup rather than from an ECS secrets reference, so the key is absent
from the repository, the image, the task definition and Terraform state.
"""

from __future__ import annotations

import functools
import json
import os
from typing import TYPE_CHECKING, Any

from config import CONFIG

if TYPE_CHECKING:
    from langchain_core.language_models.chat_models import BaseChatModel

# Set by tests to bypass provider construction entirely. Nothing in production sets this.
_OVERRIDE: BaseChatModel | None = None


def set_override(model: BaseChatModel | None) -> None:
    """Install a fake chat model. Used by the test suite and by nothing else."""
    global _OVERRIDE
    _OVERRIDE = model


@functools.lru_cache(maxsize=1)
def _openai_api_key() -> str:
    """Fetch the OpenAI key once, from Secrets Manager on AWS or the environment locally."""
    if CONFIG.openai_secret_arn:
        import boto3

        client = boto3.client("secretsmanager", region_name=CONFIG.aws_region)
        raw = client.get_secret_value(SecretId=CONFIG.openai_secret_arn)["SecretString"]
        # Accept either a bare key or a JSON object, since the documented CLI command that
        # sets this out of band can reasonably produce either.
        try:
            return str(json.loads(raw)["api_key"])
        except (json.JSONDecodeError, KeyError, TypeError):
            return raw.strip()

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "llm_provider is openai but neither OPENAI_SECRET_ARN nor OPENAI_API_KEY is set"
        )
    return key


def get_chat_model(**kwargs: Any) -> BaseChatModel:
    """Build the chat model for the configured provider.

    Callers pass provider agnostic options only, such as temperature. Anything provider
    specific belongs in here, not at the call site.
    """
    if _OVERRIDE is not None:
        return _OVERRIDE

    # Classification and extraction are not creative tasks: the same document should produce
    # the same answer. Left unset, the provider's default temperature applies, and running one
    # real invoice three times gave two different outcomes. Zero is the default here, in one
    # place, and a caller that genuinely wants variety can still pass its own.
    kwargs.setdefault("temperature", 0)

    from langchain.chat_models import init_chat_model

    provider = CONFIG.llm_provider.lower()

    if provider == "bedrock":
        from botocore.config import Config as BotoConfig

        # Retries reach the Bedrock client through botocore rather than a max_retries
        # argument. Without this, LLM_MAX_RETRIES silently did nothing in the default mode
        # and only worked in the OpenAI fallback, which is the kind of gap that is invisible
        # until a throttled call is not retried in production.
        #
        # botocore counts total attempts, not retries, so the configured retry count is
        # incremented by one to mean the same thing in both providers. Adaptive mode adds
        # client side rate limiting, which is the right behaviour against a token quota.
        return init_chat_model(
            CONFIG.llm_model_id,
            model_provider="bedrock_converse",
            region_name=CONFIG.aws_region,
            config=BotoConfig(
                retries={"max_attempts": CONFIG.llm_max_retries + 1, "mode": "adaptive"}
            ),
            **kwargs,
        )

    if provider == "openai":
        return init_chat_model(
            CONFIG.llm_model_id,
            model_provider="openai",
            api_key=_openai_api_key(),
            max_retries=CONFIG.llm_max_retries,
            **kwargs,
        )

    raise ValueError(f"unknown llm_provider {CONFIG.llm_provider!r}, expected bedrock or openai")
