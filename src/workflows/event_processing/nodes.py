"""LangGraph nodes: classification, planning, validation, execution, and output."""

from datetime import datetime
import os
from pathlib import Path
import time as clock

from domain.cases import CaseLabel, STATUS_BY_LABEL
from models.event import Event
from models.orders import Order, validate_order
from agents.call_outcome_classifier.agent import ClassifierUnavailable, classify
from agents.call_outcome_classifier.client import load_definition
from infrastructure.sqlite_store import Store
from domain.planning import build_order, plan_orders as plan_case_orders
from domain.scheduling import is_in_call_window
from domain.decision import build_decision

CALL_ROUTES = {"call", "message", "redelivery", "foreign_organization"}
OUTBOUND_OPERATIONS = {
    "programar_llamada",
    "enviar_plantilla_whatsapp",
}


def _open_store(state) -> Store:
    return Store(Path(state["store_path"]))


def load_event(state):
    """Load and validate the event before any persistent state is consulted."""
    state["event"] = Event.from_file(
        state["event_path"], state["config"].event_schema_path
    )
    definition = load_definition()
    state["prompt_version"] = definition["prompt"]["version"]
    state["model_name"] = os.getenv(definition["model"]["env"], "gpt-5.6-luna")
    state["processing_started_at"] = clock.perf_counter()
    return state


def triage_event(state):
    """Choose call, message, redelivery, or foreign-organization route."""
    event = state["event"]
    store = _open_store(state)
    try:
        state["context"] = {
            "previous_attempts": store.get_attempts(event.lead.contact_id),
            "previous_cut_offs": store.get_cut_off_count(event.lead.contact_id),
        }
        if event.organization_id != state["config"].organization_id:
            state["route"] = "foreign_organization"
        elif store.get_processed(event.idempotency_key):
            state["route"] = "redelivery"
        elif event.type == "message.received":
            state["route"] = "message"
        else:
            state["route"] = "call"
    finally:
        store.close()
    return state


def classify_signaling(state):
    """Resolve all reliable SIP, AMD, and agent-outcome classifications."""
    event = state["event"]
    telephony = event.telephony
    outcome = event.agent_outcome
    if telephony is None or outcome is None:
        state["classification_error"] = "missing call data"
        return state

    label = None
    reason = ""
    if telephony.sip_status_code == 486:
        label, reason = CaseLabel.BUSY, "SIP 486 Busy Here"
    elif telephony.sip_status_code in (408, 480):
        label, reason = (
            CaseLabel.NO_ANSWER,
            f"SIP {telephony.sip_status_code} sin respuesta",
        )
    elif telephony.sip_status_code == 603:
        label, reason = CaseLabel.REJECTED, "Rechazo activo SIP 603"
    elif 500 <= telephony.sip_status_code < 600:
        label, reason = CaseLabel.OTHER, "Fallo de trunk"
    elif telephony.amd and telephony.amd.result in {
        "machine-vm",
        "machine-unavailable",
    }:
        label, reason = CaseLabel.VOICEMAIL, "Detección de buzón"
    elif telephony.amd and telephony.amd.result == "machine-ivr":
        label, reason = CaseLabel.OTHER, "El detector identificó un IVR"
    elif outcome.appointment is not None:
        label, reason = CaseLabel.RESERVED_VISIT, "Cita creada durante la llamada"
    elif outcome.call_outcome == "dnc":
        label, reason = CaseLabel.DO_NOT_CONTACT, "El lead pidió no contactar"
    elif outcome.call_outcome == "callback_requested":
        label, reason = CaseLabel.CALLBACK, "El lead pidió callback"
    elif outcome.reason == "wrong_person":
        label, reason = CaseLabel.WRONG_PERSON, "Persona equivocada"
    elif _is_discarded(outcome.reason, event.transcript):
        label, reason = CaseLabel.DISCARDED, "El lead ya no busca la vivienda"
    elif not event.transcript or (event.quality and event.quality.no_conversation):
        label, reason = CaseLabel.OTHER, "No hubo conversación clasificable"

    if label is not None:
        state.update(
            label=label,
            reason=reason,
            confidence=1.0,
            origin="signaling",
            extracted=_extract_slots(event),
        )
    return state


def classify_llm(state):
    """Run structured conversation classification; capture provider failures safely."""
    try:
        result = classify(state["event"], state["config"].prompt_path)
    except ClassifierUnavailable as error:
        state["classification_error"] = str(error)
        state["origin"] = "degraded"
        return state
    state.update(
        classification=result,
        label=CaseLabel(result.label),
        reason=result.reason,
        confidence=result.confidence,
        extracted={
            "callback_when_raw": result.callback_when_raw,
            "callback_day": result.callback_day,
            "callback_time": result.callback_time,
            "declared_email": result.declared_email,
        },
        origin="llm",
    )
    return state


def route_after_signaling(state):
    """Route closed deterministic outcomes, conversations, and empty calls."""
    if state.get("label") is not None:
        return "plan"
    if state["event"].transcript:
        return "llm"
    return "degrade"


def plan_orders(state):
    """Create domain orders without writing side effects."""
    store = _open_store(state)
    try:
        return _plan_orders(state, store)
    finally:
        store.close()


def _plan_orders(state, store):
    """Implement planning with a short-lived SQLite read connection."""
    event = state["event"]
    route = state["route"]
    if route == "foreign_organization":
        state.update(
            label=CaseLabel.NOT_APPLICABLE,
            reason="Organización ajena",
            confidence=1.0,
            orders=[],
            origin="triage",
        )
    elif route == "redelivery":
        original = store.get_processed(event.idempotency_key)
        state.update(
            label=CaseLabel(original["etiqueta"]),
            reason=original["motivo"],
            confidence=original["confianza"],
            orders=[],
            origin="redelivery",
            original_order_ids=original.get("ordenes", []),
        )
    elif route == "message":
        orders: list[Order] = []
        for reminder_id in store.get_pending_reminders(event.lead.contact_id):
            orders.append(
                build_order(
                    event,
                    "cancelar_recordatorio",
                    {"reminder_id": reminder_id, "motivo": "Lead respondió"},
                    reminder_id,
                )
            )
        state.update(
            label=CaseLabel.NOT_APPLICABLE,
            reason="Respuesta del lead",
            confidence=1.0,
            orders=orders,
            origin="message",
        )
    else:
        if state.get("classification_error"):
            state["orders"] = []
            return state
        try:
            state["orders"] = plan_case_orders(
                event,
                state["label"],
                state["reason"],
                state["confidence"],
                state["config"],
                state["context"]["previous_attempts"] + 1,
                store,
                state.get("extracted"),
                state["context"]["previous_cut_offs"],
            )
        except (TypeError, ValueError) as error:
            state["orders"] = []
            state["validation_error"] = f"plan inválido: {error}"
    return state


def validate_orders(state):
    """Validate CRM bodies and the domain invariants before any write."""
    if state.get("validation_error"):
        return state
    try:
        for order in state.get("orders", []):
            validate_order(order)
        _validate_invariants(state)
        state["validation_error"] = None
    except (AssertionError, TypeError, ValueError) as error:
        state["validation_error"] = str(error) or "invalid order set"
    return state


def route_after_validation(state):
    """Select execution only when every body and invariant is valid."""
    return "degrade" if state.get("validation_error") else "execute"


def route_after_llm(state):
    """Send provider failures to the safe review branch."""
    return "degrade" if state.get("classification_error") else "plan"


def degrade_to_review(state):
    """Create only internal review work after a classification or validation failure."""
    event = state["event"]
    reason = (
        state.get("validation_error")
        or state.get("classification_error")
        or state.get("error")
        or "clasificación no disponible"
    )
    state.update(
        label=CaseLabel.OTHER,
        reason=f"Revisión necesaria: {reason}",
        confidence=0.0,
        origin="degraded",
        orders=[
            build_order(
                event,
                "cerrar_llamada",
                {
                    "entry_id": event.campaign.entry_id,
                    "status": "needs_review",
                    "etiqueta": CaseLabel.OTHER.value,
                    "motivo": f"Revisión necesaria: {reason}",
                    "confianza": 0.0,
                    "duration_seconds": (
                        event.telephony.duration_seconds if event.telephony else 0
                    ),
                },
                "degraded-close",
            ),
            build_order(
                event,
                "crear_tarea",
                {
                    "contact_id": event.lead.contact_id,
                    "call_id": event.telephony.call_id if event.telephony else None,
                    "tipo": "revisar_llamada",
                    "titulo": "Revisar llamada",
                    "detalle": f"Revisión necesaria: {reason}",
                    "vence_el": event.occurred_at.isoformat(),
                    "asignada_a": "cualquiera",
                },
                "degraded-review",
            ),
        ],
    )
    return state


def execute(state):
    """Persist orders and their idempotent domain effects."""
    duration_ms = (clock.perf_counter() - state["processing_started_at"]) * 1000
    store = _open_store(state)
    try:
        store.persist_effects(
            state["event"],
            state.get("orders", []),
            Path(state["output_path"]),
            state.get("origin", "unknown"),
            state.get("model_name", "gpt-5.6-luna"),
            state.get("prompt_version", "v1"),
            duration_ms,
            state.get("label"),
            state["route"] == "call",
        )
    finally:
        store.close()
    return state


def persist_decision(state):
    """Append the delivery decision after order effects are durable."""
    event = state["event"]
    orders = state.get("orders", [])
    decision = build_decision(
        event,
        state["label"].value,
        state["reason"],
        state["confidence"],
        orders,
        state.get("origin", "unknown"),
    )
    # A redelivery must reproduce the original order references, not create new ones.
    if state.get("original_order_ids") is not None:
        decision["ordenes"] = state["original_order_ids"]
    state["decision"] = decision
    store = _open_store(state)
    try:
        store.persist_decision(
            event,
            decision,
            Path(state["output_path"]),
            state["route"] not in {"redelivery", "foreign_organization"},
        )
    finally:
        store.close()
    return state


def _extract_slots(event: Event) -> dict:
    return dict(event.agent_outcome.slots_snapshot) if event.agent_outcome else {}


def _is_discarded(reason: str | None, transcript) -> bool:
    text = " ".join(line.message for line in (transcript or [])).lower()
    value = f"{reason or ''} {text}"
    return any(
        phrase in value
        for phrase in (
            "compró",
            "compro",
            "comprado",
            "alquiló",
            "alquilo",
            "alquilado",
            "ya no busco",
            "already_bought",
            "already_rented",
        )
    )


def _validate_invariants(state) -> None:
    event = state["event"]
    route = state["route"]
    label = state.get("label")
    orders = state.get("orders", [])
    operations = [order.operation for order in orders]

    if route == "call" and event.type == "call.ended":
        assert "cerrar_llamada" in operations, "call must close its queue entry"
    if route in {"message", "foreign_organization", "redelivery"}:
        assert "cerrar_llamada" not in operations, "non-call branch cannot close a call"
    if label is CaseLabel.DO_NOT_CONTACT:
        assert set(operations) <= {"cerrar_llamada", "marcar_no_contactar"}

    store = _open_store(state)
    try:
        contact_id = event.lead.contact_id
        if store.is_do_not_contact(contact_id):
            assert not any(operation in OUTBOUND_OPERATIONS for operation in operations)
            assert not any(
                order.operation == "programar_recordatorio"
                and order.body.get("canal") == "whatsapp_lead"
                for order in orders
            )
        if not store.is_channel_allowed(contact_id, "whatsapp"):
            assert not any(
                order.operation == "enviar_plantilla_whatsapp"
                or (
                    order.operation == "programar_recordatorio"
                    and order.body.get("canal") == "whatsapp_lead"
                )
                for order in orders
            )
    finally:
        store.close()

    effective_attempt = state.get("context", {}).get("previous_attempts", 0) + 1
    max_attempts = state["config"].raw["reintentos"]["max_intentos"]
    if effective_attempt >= max_attempts:
        assert "programar_llamada" not in operations
    for order in orders:
        if order.operation == "programar_llamada":
            scheduled = datetime.fromisoformat(order.body["no_antes_de"])
            assert is_in_call_window(scheduled, state["config"])

    if label in STATUS_BY_LABEL:
        close_orders = [
            order for order in orders if order.operation == "cerrar_llamada"
        ]
        if close_orders:
            assert close_orders[0].body["status"] == STATUS_BY_LABEL[label]
