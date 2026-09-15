"""Typed state passed between LangGraph nodes."""

from typing import Any, TypedDict


class OrchestratorState(TypedDict, total=False):
    """Serializable values shared by the graph's load, plan, and write nodes."""

    event_path: str
    event: Any
    config: Any
    store_path: str
    output_path: str
    route: str
    context: dict
    classification: Any
    label: Any
    reason: str
    confidence: float
    orders: list
    decision: dict
    error: str
    extracted: dict
    origin: str
    validation_error: str | None
    processing_started_at: float
    prompt_version: str
    model_name: str
    original_order_ids: list[str]
