"""OpenAI client construction from the versioned classifier definition."""

import json
import os
from pathlib import Path

from config import load_environment
from models.classification import Classification


def load_definition() -> dict:
    """Load the versioned model and prompt metadata."""
    return json.loads(
        (Path(__file__).parent / "definition.json").read_text(encoding="utf-8")
    )


def structured_model():
    """Build the configured structured-output client for live classification."""
    from langchain_openai import ChatOpenAI

    load_environment()
    definition = load_definition()
    model_config = definition["model"]
    model_kwargs = {}
    if "reasoning_effort" in model_config:
        model_kwargs["reasoning_effort"] = model_config["reasoning_effort"]
    return ChatOpenAI(
        model=os.getenv(model_config["env"], "gpt-5.6-luna"),
        temperature=model_config.get("temperature", 0),
        model_kwargs=model_kwargs,
        timeout=model_config.get("timeout_seconds", 30),
        max_retries=model_config.get("max_retries", 2),
    ).with_structured_output(Classification)
