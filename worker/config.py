"""Runtime configuration, read once from the environment.

Every value that differs between local runs, the compose stack and AWS is here, so no
module reads os.environ directly. Terraform sets these on the ECS task definition.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    # LLM provider selection. The graph, the prompts and the output schemas are identical
    # across providers, so switching is a variable change and a deploy, with no code change.
    llm_provider: str = os.environ.get("LLM_PROVIDER", "bedrock")
    llm_model_id: str = os.environ.get("LLM_MODEL_ID", "apac.amazon.nova-pro-v1:0")
    aws_region: str = os.environ.get("AWS_REGION", "ap-southeast-1")

    # Where the OpenAI key lives in fallback mode. Read with boto3 at startup, never
    # injected as an ECS secrets reference and never present in Terraform state.
    openai_secret_arn: str | None = os.environ.get("OPENAI_SECRET_ARN")

    # Routing thresholds. Architecture section 5 calls the first one configurable.
    classify_confidence_threshold: float = _float("CLASSIFY_CONFIDENCE_THRESHOLD", 0.60)
    # 2 means one extraction, then at most one retry with the validation errors appended.
    max_extraction_attempts: int = _int("MAX_EXTRACTION_ATTEMPTS", 2)

    # Model input limits, enforced before the call rather than discovered during it.
    max_pages: int = _int("MAX_PAGES", 20)
    max_image_edge_px: int = _int("MAX_IMAGE_EDGE_PX", 1568)
    # Below this many characters a PDF is treated as scanned and rendered to images.
    scanned_text_threshold: int = _int("SCANNED_TEXT_THRESHOLD", 200)

    # Transient error retries inside the LLM client, before the exception propagates and
    # SQS redelivers the message.
    llm_max_retries: int = _int("LLM_MAX_RETRIES", 3)


CONFIG = Config()
