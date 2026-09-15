"""Auditable decision output built from event evidence and planned orders."""

from models.event import Event
from models.orders import Order


def build_reasoning(
    event: Event,
    label: str,
    reason: str,
    confidence: float,
    orders: list[Order],
    origin: str,
) -> str:
    """Create a detailed evidence summary without exposing private model reasoning."""
    facts: list[str] = [
        f"Se revisó el evento {event.event_id} de tipo {event.type}, "
        f"de la organización {event.organization_id}, con clave de idempotencia "
        f"{event.idempotency_key}.",
        f"El origen de la clasificación fue {origin} y la etiqueta seleccionada "
        f"es «{label}» con confianza {confidence:.2f}.",
    ]

    if event.type == "call.ended" and event.telephony:
        telephony = event.telephony
        facts.append(
            f"La señalización indica SIP {telephony.sip_status_code} "
            f"({telephony.sip_status or 'sin texto'}), desconexión "
            f"{telephony.disconnect_reason}, duración de "
            f"{telephony.duration_seconds} segundos y "
            f"{('llamada contestada' if telephony.answered_at else 'llamada no contestada')}."
        )
        if telephony.amd:
            facts.append(
                f"La detección de contestador fue «{telephony.amd.result or 'sin dato'}» "
                f"mediante {telephony.amd.source or 'sin fuente'}"
                + (
                    f", con saludo «{telephony.amd.greeting_transcript}»."
                    if telephony.amd.greeting_transcript
                    else "."
                )
            )
        if event.transcript:
            facts.append(
                f"La transcripción contiene {len(event.transcript)} turnos y se usó "
                "para contrastar la señalización con lo ocurrido en la conversación."
            )
        else:
            facts.append("No hay transcripción conversacional que añadir al análisis.")
        if event.agent_outcome:
            outcome = event.agent_outcome
            facts.append(
                f"El desenlace determinista del agente es «{outcome.call_outcome or 'vacío'}»"
                + (f", con motivo técnico «{outcome.reason}»" if outcome.reason else "")
                + "."
            )
            if outcome.appointment:
                facts.append(
                    f"Además, el agente creó la cita {outcome.appointment.appointment_id} "
                    f"para {outcome.appointment.start_time.isoformat()}."
                )
            if outcome.slots_snapshot:
                facts.append(
                    f"Se conservaron {len(outcome.slots_snapshot)} campos de contexto "
                    "recogidos durante la llamada."
                )
    elif event.message:
        facts.append(
            f"El lead envió un mensaje entrante por {event.message.channel}; "
            "se trató como respuesta del lead y no como una nueva llamada."
        )

    facts.append(f"Motivo operativo: {reason}.")
    if orders:
        operations = ", ".join(order.operation for order in orders)
        facts.append(f"Las acciones resultantes son: {operations}.")
    else:
        facts.append("No se emitieron órdenes para este evento.")
    return " ".join(facts)


def build_decision(
    event: Event,
    label: str,
    reason: str,
    confidence: float,
    orders: list[Order],
    origin: str,
) -> dict:
    """Build the compatible decision row plus a complete event snapshot."""
    return {
        "event_id": event.event_id,
        "call_id": event.telephony.call_id if event.telephony else None,
        "etiqueta": label,
        "motivo": reason,
        "confianza": confidence,
        "ordenes": [order.order_id for order in orders],
        "razonamiento": build_reasoning(
            event, label, reason, confidence, orders, origin
        ),
        "evento": event.output_dict(),
    }
