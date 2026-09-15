"""Explicit LangGraph topology for one event."""

from langgraph.graph import END, StateGraph

from .nodes import (
    classify_llm,
    classify_signaling,
    degrade_to_review,
    execute,
    load_event,
    persist_decision,
    plan_orders,
    route_after_llm,
    route_after_signaling,
    route_after_validation,
    triage_event,
    validate_orders,
)
from .state import OrchestratorState


def build_graph(checkpointer=None):
    """Build the explicit event graph, optionally with a SQLite checkpointer."""
    graph = StateGraph(OrchestratorState)
    graph.add_node("load_event", load_event)
    graph.add_node("triage_event", triage_event)
    graph.add_node("classify_signaling", classify_signaling)
    graph.add_node("classify_llm", classify_llm)
    graph.add_node("plan_orders", plan_orders)
    graph.add_node("validate_orders", validate_orders)
    graph.add_node("degrade_to_review", degrade_to_review)
    graph.add_node("execute", execute)
    graph.add_node("persist_decision", persist_decision)

    graph.set_entry_point("load_event")
    # Triage has four domain routes; keeping them as graph edges makes the
    # no-contact and redelivery contracts visible to reviewers.
    graph.add_edge("load_event", "triage_event")
    graph.add_conditional_edges(
        "triage_event",
        lambda state: state["route"],
        {
            "call": "classify_signaling",
            "message": "plan_orders",
            "redelivery": "plan_orders",
            "foreign_organization": "plan_orders",
        },
    )
    graph.add_conditional_edges(
        "classify_signaling",
        route_after_signaling,
        {"plan": "plan_orders", "llm": "classify_llm", "degrade": "degrade_to_review"},
    )
    # Provider errors and invalid plans converge on a review-only branch.
    graph.add_conditional_edges(
        "classify_llm",
        route_after_llm,
        {"plan": "plan_orders", "degrade": "degrade_to_review"},
    )
    graph.add_edge("plan_orders", "validate_orders")
    graph.add_conditional_edges(
        "validate_orders",
        route_after_validation,
        {"execute": "execute", "degrade": "degrade_to_review"},
    )
    graph.add_edge("degrade_to_review", "execute")
    graph.add_edge("execute", "persist_decision")
    graph.add_edge("persist_decision", END)
    return graph.compile(checkpointer=checkpointer)
